"""Energy Saving engine: the configuration and state of the three levels.

It has no HA imports. It validates and stores each level's configuration,
the active level, releases, Smart room occupancy and an event history;
energy_adapter drives the devices and energy_runtime and energy_api expose
it. A release is a device someone changed by hand while a level was active:
the adapter leaves it alone until a re-apply, or in Smart until its room is
empty. Methods are synchronous and run in HA's executor. Each change is
written to SQLite before memory, so a failed write leaves the two matching.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

LEVEL_LOW = "low"
LEVEL_MEDIUM = "medium"
LEVEL_SMART = "smart"
ENERGY_LEVELS = (LEVEL_LOW, LEVEL_MEDIUM, LEVEL_SMART)

CONFIG_SCHEMA_VERSION = 1

EVENT_CONFIG_UPDATED = "config_updated"
EVENT_CONFIG_RESET = "config_reset"
EVENT_ACTIVATED = "activated"
EVENT_DEACTIVATED = "deactivated"
EVENT_REAPPLIED = "reapplied"
EVENT_RELEASED = "released"
EVENT_RELEASES_CLEARED = "releases_cleared"
EVENT_OCCUPANCY_CHANGED = "occupancy_changed"

# The state table's only key.
_STATE_KEY = "current"
# Event history older than this is pruned at boot.
_EVENT_RETENTION_SECONDS = 180 * 24 * 3600
# Bounds on stored ids and lists, so a request cannot grow the documents
# without limit.
_MAX_ID_LENGTH = 255
_MAX_SOURCE_LENGTH = 64
_MAX_LIST_ITEMS = 1024
_MAX_PICK_GROUPS = 512

_COMMON_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "excluded_rooms",
        "gang_keepers",
        "light_keepers",
        "plug_offs",
        "heaters",
        "ac_keepers",
        "setup_complete",
    }
)
# Low and Medium always lock family controls; only Smart makes it a choice.
_SMART_CONFIG_FIELDS = _COMMON_CONFIG_FIELDS | {"lockout_enabled"}


class EnergyError(Exception):
    """Base class for rejected Energy Saving operations."""


class EnergyConfigError(EnergyError):
    """A level configuration or pick has an invalid shape/value."""


class UnknownEnergyLevelError(EnergyError):
    """The requested level is not Low, Medium, or Smart."""


class EnergySetupRequiredError(EnergyError):
    """Activation was attempted before that level's wizard was completed."""

    def __init__(self, level: str) -> None:
        super().__init__(f"{level} setup is not complete")
        self.level = level


class EnergyAlreadyActiveError(EnergyError):
    """A level is already active; it must be deactivated explicitly."""


class EnergyInactiveError(EnergyError):
    """An active-level-only operation was requested while inactive."""


# -- validation helpers -------------------------------------------------------


def _validate_level(level: Any) -> str:
    """Return the level if it is one of the three, else raise."""
    if level not in ENERGY_LEVELS:
        raise UnknownEnergyLevelError(
            f"Unknown energy level {level!r} (expected one of {ENERGY_LEVELS})"
        )
    return level


def _clean_id(value: Any, field: str) -> str:
    """A stripped, non-empty, bounded string id."""
    if not isinstance(value, str) or not value.strip():
        raise EnergyConfigError(f"{field} must be a non-empty string")
    clean = value.strip()
    if len(clean) > _MAX_ID_LENGTH:
        raise EnergyConfigError(f"{field} must be <= {_MAX_ID_LENGTH} characters")
    return clean


def _clean_entity_id(value: Any, field: str) -> str:
    """A clean id that is also shaped like domain.object_id."""
    clean = _clean_id(value, field)
    domain, separator, object_id = clean.partition(".")
    if not separator or not domain or not object_id:
        raise EnergyConfigError(
            f"{field} must be a Home Assistant entity_id (domain.object_id)"
        )
    return clean


def _clean_unique_list(
    value: Any,
    field: str,
    *,
    entity_ids: bool,
    allow_empty: bool = True,
) -> list[str]:
    """A bounded list of clean ids (or entity ids) with no duplicates."""
    if not isinstance(value, list):
        raise EnergyConfigError(f"{field} must be an array")
    if len(value) > _MAX_LIST_ITEMS:
        raise EnergyConfigError(f"{field} may contain at most {_MAX_LIST_ITEMS} items")
    cleaner = _clean_entity_id if entity_ids else _clean_id
    clean: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        normalized = cleaner(item, f"{field}[{index}]")
        if normalized in seen:
            raise EnergyConfigError(f"{field} contains duplicate value {normalized!r}")
        seen.add(normalized)
        clean.append(normalized)
    if not allow_empty and not clean:
        raise EnergyConfigError(f"{field} must contain at least one item")
    return clean


def _clean_pick_map(
    value: Any,
    field: str,
    *,
    exact_count: int | None,
    allow_empty_picks: bool,
) -> dict[str, list[str]]:
    """A keeper map of group or room id to entity ids.

    When exact_count is set, every group must name that many keepers.
    """
    if not isinstance(value, dict):
        raise EnergyConfigError(f"{field} must be an object")
    if len(value) > _MAX_PICK_GROUPS:
        raise EnergyConfigError(
            f"{field} may contain at most {_MAX_PICK_GROUPS} groups"
        )
    clean: dict[str, list[str]] = {}
    for raw_group_id, raw_picks in value.items():
        group_id = _clean_id(raw_group_id, f"{field} group id")
        picks = _clean_unique_list(
            raw_picks,
            f"{field}.{group_id}",
            entity_ids=True,
            allow_empty=allow_empty_picks,
        )
        if exact_count is not None and len(picks) != exact_count:
            raise EnergyConfigError(
                f"{field}.{group_id} must contain exactly "
                f"{exact_count} keeper{'s' if exact_count != 1 else ''}"
            )
        clean[group_id] = picks
    return clean


def _clean_heaters(value: Any) -> list[dict[str, Any]]:
    """The heater list: one {entity_id, turn_off} item per entity."""
    if not isinstance(value, list):
        raise EnergyConfigError("heaters must be an array")
    if len(value) > _MAX_LIST_ITEMS:
        raise EnergyConfigError(f"heaters may contain at most {_MAX_LIST_ITEMS} items")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise EnergyConfigError(f"heaters[{index}] must be an object")
        unknown = set(raw) - {"entity_id", "turn_off"}
        if unknown:
            raise EnergyConfigError(
                f"heaters[{index}] has unknown fields: {sorted(unknown)}"
            )
        entity_id = _clean_entity_id(
            raw.get("entity_id"), f"heaters[{index}].entity_id"
        )
        if entity_id in seen:
            raise EnergyConfigError(f"heaters contains duplicate entity {entity_id!r}")
        turn_off = raw.get("turn_off")
        if not isinstance(turn_off, bool):
            raise EnergyConfigError(f"heaters[{index}].turn_off must be a boolean")
        seen.add(entity_id)
        clean.append({"entity_id": entity_id, "turn_off": turn_off})
    return clean


# -- level configuration ------------------------------------------------------


def default_level_config(level: str) -> dict[str, Any]:
    """A fresh default configuration for the level."""
    level = _validate_level(level)
    config: dict[str, Any] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "excluded_rooms": [],
        "gang_keepers": {},
        "light_keepers": {},
        "plug_offs": [],
        "heaters": [],
        "ac_keepers": {},
        "setup_complete": False,
    }
    if level == LEVEL_SMART:
        config["lockout_enabled"] = True
    return config


def validate_level_config(level: str, value: Any) -> dict[str, Any]:
    """Validate and normalize a complete per-level configuration document.

    Keeper counts that the document alone can show are checked here: Low
    gang groups keep two, Medium and Smart groups one, and Medium rooms with
    several ACs one. energy_validation checks the picks against the live
    discovery, including the ceil(n/2) light counts.
    """
    level = _validate_level(level)
    if not isinstance(value, dict):
        raise EnergyConfigError("configuration must be an object")
    allowed = _SMART_CONFIG_FIELDS if level == LEVEL_SMART else _COMMON_CONFIG_FIELDS
    unknown = set(value) - allowed
    if unknown:
        raise EnergyConfigError(
            f"{level} configuration has unknown fields: {sorted(unknown)}"
        )

    schema_version = value.get("schema_version", CONFIG_SCHEMA_VERSION)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CONFIG_SCHEMA_VERSION
    ):
        raise EnergyConfigError(f"schema_version must be {CONFIG_SCHEMA_VERSION}")

    setup_complete = value.get("setup_complete", False)
    if not isinstance(setup_complete, bool):
        raise EnergyConfigError("setup_complete must be a boolean")

    excluded_rooms = _clean_unique_list(
        value.get("excluded_rooms", []),
        "excluded_rooms",
        entity_ids=False,
    )
    gang_keepers = _clean_pick_map(
        value.get("gang_keepers", {}),
        "gang_keepers",
        exact_count=2 if level == LEVEL_LOW else 1,
        allow_empty_picks=False,
    )
    light_keepers = _clean_pick_map(
        value.get("light_keepers", {}),
        "light_keepers",
        exact_count=None,
        allow_empty_picks=False,
    )
    plug_offs = _clean_unique_list(
        value.get("plug_offs", []),
        "plug_offs",
        entity_ids=True,
    )
    heaters = _clean_heaters(value.get("heaters", []))

    raw_ac_keepers = value.get("ac_keepers", {})
    if level == LEVEL_LOW and raw_ac_keepers:
        raise EnergyConfigError("Low does not accept AC keeper picks")
    ac_keepers = _clean_pick_map(
        raw_ac_keepers,
        "ac_keepers",
        exact_count=1 if level == LEVEL_MEDIUM else None,
        # A Smart room without sensors may keep none of its ACs.
        allow_empty_picks=level == LEVEL_SMART,
    )

    excluded = set(excluded_rooms)
    configured_excluded = excluded.intersection(set(light_keepers) | set(ac_keepers))
    if configured_excluded:
        raise EnergyConfigError(
            "excluded rooms cannot also carry light/AC picks: "
            f"{sorted(configured_excluded)}"
        )

    normalized: dict[str, Any] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "excluded_rooms": excluded_rooms,
        "gang_keepers": gang_keepers,
        "light_keepers": light_keepers,
        "plug_offs": plug_offs,
        "heaters": heaters,
        "ac_keepers": ac_keepers,
        "setup_complete": setup_complete,
    }
    if level == LEVEL_SMART:
        lockout_enabled = value.get("lockout_enabled", True)
        if not isinstance(lockout_enabled, bool):
            raise EnergyConfigError("lockout_enabled must be a boolean")
        normalized["lockout_enabled"] = lockout_enabled
    return normalized


# -- engine -------------------------------------------------------------------


class EnergyEngine:
    """Persistent Energy Saving configuration and state.

    config_table holds one document per level and state_table one "current"
    document; events is storage.energy_events(). Activation, deactivation and
    re-apply only record state: EnergyController runs the adapter around them.
    """

    def __init__(
        self,
        config_table: Any,
        state_table: Any,
        events: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config_table = config_table
        self._state_table = state_table
        self._events = events
        self._clock = clock
        self._lock = threading.RLock()
        self._configs = {level: default_level_config(level) for level in ENERGY_LEVELS}
        self._state = self._default_state()

    # -- lifecycle ---------------------------------------------------------

    def warm_up(self) -> None:
        """Load and validate the stored documents into memory.

        A malformed level config falls back to that level's defaults, and a
        malformed state document keeps only what still validates.
        """
        with self._lock:
            for level in ENERGY_LEVELS:
                stored = self._config_table.get(level)
                if stored is None:
                    self._configs[level] = default_level_config(level)
                    continue
                try:
                    self._configs[level] = validate_level_config(level, stored)
                except EnergyConfigError as err:
                    _LOGGER.error(
                        "Ignoring invalid persisted %s energy config: %s",
                        level,
                        err,
                    )
                    self._configs[level] = default_level_config(level)
            self._state = self._coerce_state(self._state_table.get(_STATE_KEY))
            # The event history is only an audit log; trim it at boot.
            now = self._now()
            self._events.prune(before_t=max(0, now - _EVENT_RETENTION_SECONDS))

    # -- configuration -----------------------------------------------------

    def get_config(self, level: str) -> dict[str, Any]:
        """A copy of one level's configuration."""
        level = _validate_level(level)
        with self._lock:
            return copy.deepcopy(self._configs[level])

    def replace_config(
        self,
        level: str,
        config: Any,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Validate and replace one complete level document."""
        level = _validate_level(level)
        normalized = validate_level_config(level, config)
        clean_actor = self._optional_actor(actor)
        now = self._now()
        with self._lock:
            self._config_table[level] = copy.deepcopy(normalized)
            self._configs[level] = normalized
            self._record_event(
                EVENT_CONFIG_UPDATED,
                level=level,
                data={
                    "changed_fields": sorted(normalized),
                    "setup_complete": normalized["setup_complete"],
                    "actor": clean_actor,
                },
                t=now,
            )
            return copy.deepcopy(normalized)

    def patch_config(
        self,
        level: str,
        patch: Any,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Merge a wizard step into a level's configuration and store it."""
        level = _validate_level(level)
        if not isinstance(patch, dict):
            raise EnergyConfigError("configuration patch must be an object")
        allowed = (
            _SMART_CONFIG_FIELDS if level == LEVEL_SMART else _COMMON_CONFIG_FIELDS
        )
        unknown = set(patch) - allowed
        if unknown:
            raise EnergyConfigError(
                f"{level} configuration has unknown fields: {sorted(unknown)}"
            )
        clean_actor = self._optional_actor(actor)
        now = self._now()
        with self._lock:
            merged = copy.deepcopy(self._configs[level])
            merged.update(copy.deepcopy(patch))
            normalized = validate_level_config(level, merged)
            self._config_table[level] = copy.deepcopy(normalized)
            self._configs[level] = normalized
            self._record_event(
                EVENT_CONFIG_UPDATED,
                level=level,
                data={
                    "changed_fields": sorted(patch),
                    "setup_complete": normalized["setup_complete"],
                    "actor": clean_actor,
                },
                t=now,
            )
            return copy.deepcopy(normalized)

    def reset_config(self, level: str, *, actor: str | None = None) -> dict[str, Any]:
        """Reset one level's configuration to the defaults."""
        level = _validate_level(level)
        clean_actor = self._optional_actor(actor)
        now = self._now()
        with self._lock:
            config = default_level_config(level)
            self._config_table[level] = copy.deepcopy(config)
            self._configs[level] = config
            self._record_event(
                EVENT_CONFIG_RESET,
                level=level,
                data={"actor": clean_actor},
                t=now,
            )
            return copy.deepcopy(config)

    # -- active state ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The live state for the API, read from memory."""
        with self._lock:
            state = copy.deepcopy(self._state)
            state["active"] = state["active_level"] is not None
            state["release_count"] = len(state["released_entities"])
            return state

    @property
    def active_level(self) -> str | None:
        """The active level, or None when Energy Saving is off."""
        with self._lock:
            return self._state["active_level"]

    def activate(
        self,
        level: str,
        *,
        smart_lockout_enabled: bool | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Record a newly active level; the adapter applies it to devices."""
        level = _validate_level(level)
        clean_actor = self._optional_actor(actor)
        with self._lock:
            active = self._state["active_level"]
            if active is not None:
                raise EnergyAlreadyActiveError(
                    f"{active} is already active; deactivate it first"
                )
            config = self._configs[level]
            if not config["setup_complete"]:
                raise EnergySetupRequiredError(level)
            if level != LEVEL_SMART and smart_lockout_enabled is not None:
                raise EnergyConfigError("smart_lockout_enabled applies only to Smart")
            if smart_lockout_enabled is not None and not isinstance(
                smart_lockout_enabled, bool
            ):
                raise EnergyConfigError("smart_lockout_enabled must be a boolean")
            now = self._now()
            if (
                smart_lockout_enabled is not None
                and config["lockout_enabled"] != smart_lockout_enabled
            ):
                config = copy.deepcopy(config)
                config["lockout_enabled"] = smart_lockout_enabled
                self._config_table[level] = copy.deepcopy(config)
                self._configs[level] = config

            lockout = config["lockout_enabled"] if level == LEVEL_SMART else True
            self._commit_state(
                {
                    "active_level": level,
                    "activated_at": now,
                    "last_applied_at": now,
                    "lockout_enabled": lockout,
                    "released_entities": [],
                    "release_details": {},
                    "room_occupancy": {},
                    "revision": self._state["revision"] + 1,
                }
            )
            self._record_event(
                EVENT_ACTIVATED,
                level=level,
                data={
                    "lockout_enabled": lockout,
                    "actor": clean_actor,
                },
                t=now,
            )
            return self.snapshot()

    def deactivate(self, *, actor: str | None = None) -> dict[str, Any]:
        """Record that no level is active; devices are left as they are."""
        clean_actor = self._optional_actor(actor)
        with self._lock:
            level = self._state["active_level"]
            if level is None:
                return self.snapshot()
            now = self._now()
            released_count = len(self._state["released_entities"])
            self._commit_state(
                {
                    "active_level": None,
                    "activated_at": None,
                    "last_applied_at": None,
                    "lockout_enabled": False,
                    "released_entities": [],
                    "release_details": {},
                    "room_occupancy": {},
                    "revision": self._state["revision"] + 1,
                }
            )
            self._record_event(
                EVENT_DEACTIVATED,
                level=level,
                data={
                    "released_count": released_count,
                    "restored_devices": False,
                    "actor": clean_actor,
                },
                t=now,
            )
            return self.snapshot()

    def reapply(self, *, actor: str | None = None) -> dict[str, Any]:
        """Record a re-apply of the active level, clearing every release."""
        clean_actor = self._optional_actor(actor)
        with self._lock:
            level = self._require_active()
            now = self._now()
            released_count = len(self._state["released_entities"])
            state = self._next_state()
            state["released_entities"] = []
            state["release_details"] = {}
            state["last_applied_at"] = now
            self._commit_state(state)
            self._record_event(
                EVENT_REAPPLIED,
                level=level,
                data={
                    "cleared_releases": released_count,
                    "actor": clean_actor,
                },
                t=now,
            )
            return self.snapshot()

    # -- release ledger ----------------------------------------------------

    def is_released(self, entity_id: str) -> bool:
        """True when the household has taken this device back by hand."""
        entity_id = _clean_entity_id(entity_id, "entity_id")
        with self._lock:
            return entity_id in self._state["release_details"]

    def mark_released(
        self,
        entity_id: str,
        *,
        room_id: str | None = None,
        source: str = "external",
        actor: str | None = None,
    ) -> bool:
        """Release a device while a level is active; True when newly released."""
        entity_id = _clean_entity_id(entity_id, "entity_id")
        clean_room = _clean_id(room_id, "room_id") if room_id is not None else None
        clean_source = _clean_id(source, "source")
        if len(clean_source) > _MAX_SOURCE_LENGTH:
            raise EnergyConfigError(
                f"source must be <= {_MAX_SOURCE_LENGTH} characters"
            )
        with self._lock:
            level = self._state["active_level"]
            if level is None:
                return False
            clean_actor = self._optional_actor(actor)
            if entity_id in self._state["release_details"]:
                return False
            released_at = self._now()
            state = self._next_state()
            details = state["release_details"]
            details[entity_id] = {
                "room_id": clean_room,
                "released_at": released_at,
                "source": clean_source,
            }
            state["released_entities"] = sorted(details)
            self._commit_state(state)
            self._record_event(
                EVENT_RELEASED,
                level=level,
                entity_id=entity_id,
                room_id=clean_room,
                data={
                    "source": clean_source,
                    "actor": clean_actor,
                },
                t=released_at,
            )
            return True

    def clear_room_releases(
        self, room_id: str, *, reason: str = "room_empty"
    ) -> list[str]:
        """Clear one room's Smart releases once the adapter finds it empty.

        Returns the entity ids that were cleared.
        """
        room_id = _clean_id(room_id, "room_id")
        reason = _clean_id(reason, "reason")
        with self._lock:
            if self._state["active_level"] != LEVEL_SMART:
                return []
            details = self._state["release_details"]
            cleared = sorted(
                entity_id
                for entity_id, record in details.items()
                if record.get("room_id") == room_id
            )
            if not cleared:
                return []
            now = self._now()
            state = self._next_state()
            for entity_id in cleared:
                state["release_details"].pop(entity_id, None)
            state["released_entities"] = sorted(state["release_details"])
            self._commit_state(state)
            self._record_event(
                EVENT_RELEASES_CLEARED,
                level=LEVEL_SMART,
                room_id=room_id,
                data={"entities": cleared, "reason": reason},
                t=now,
            )
            return cleared

    # -- room occupancy ----------------------------------------------------

    def set_room_occupancy(
        self,
        room_id: str,
        occupied: bool | None,
        *,
        sensors_available: bool = True,
    ) -> bool:
        """Store one Smart room's occupancy; True when it changed.

        occupied is None when sensors_available is False, and a bool
        otherwise. The adapter applies the empty grace before calling this.
        """
        room_id = _clean_id(room_id, "room_id")
        if not isinstance(sensors_available, bool):
            raise EnergyConfigError("sensors_available must be a boolean")
        if sensors_available:
            if not isinstance(occupied, bool):
                raise EnergyConfigError(
                    "occupied must be a boolean when sensors are available"
                )
        elif occupied is not None:
            raise EnergyConfigError(
                "occupied must be null when sensors are unavailable"
            )

        with self._lock:
            if self._state["active_level"] != LEVEL_SMART:
                return False
            previous = self._state["room_occupancy"].get(room_id)
            if (
                previous is not None
                and previous.get("occupied") is occupied
                and previous.get("sensors_available") is sensors_available
            ):
                return False
            now = self._now()
            record = {
                "occupied": occupied,
                "sensors_available": sensors_available,
                "changed_at": now,
            }
            state = self._next_state()
            state["room_occupancy"][room_id] = record
            self._commit_state(state)
            self._record_event(
                EVENT_OCCUPANCY_CHANGED,
                level=LEVEL_SMART,
                room_id=room_id,
                data={
                    "occupied": occupied,
                    "sensors_available": sensors_available,
                },
                t=now,
            )
            return True

    # -- events and stats ------------------------------------------------

    def record_event(
        self,
        kind: str,
        *,
        level: str | None = None,
        entity_id: str | None = None,
        room_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an event to the history (rules, automations, command gate)."""
        if level is not None:
            level = _validate_level(level)
        with self._lock:
            return self._record_event(
                kind,
                level=level,
                entity_id=entity_id,
                room_id=room_id,
                data=data,
            )

    def recent_events(
        self,
        *,
        limit: int = 100,
        since_t: int | None = None,
        kinds: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """The newest events first, optionally since a time or of some kinds."""
        return self._events.recent(limit=limit, since_t=since_t, kinds=kinds)

    def stats(self, *, since_t: int | None = None) -> dict[str, Any]:
        """Operational counts only; no estimated energy, cost or CO2 figures."""
        with self._lock:
            occupancy = self._state["room_occupancy"].values()
            stats = self._events.summary(since_t=since_t)
            stats.update(
                {
                    "active": self._state["active_level"] is not None,
                    "active_level": self._state["active_level"],
                    "released_devices": len(self._state["released_entities"]),
                    "occupied_rooms": sum(
                        1
                        for room in occupancy
                        if room["sensors_available"] and room["occupied"] is True
                    ),
                    "empty_rooms": sum(
                        1
                        for room in occupancy
                        if room["sensors_available"] and room["occupied"] is False
                    ),
                    "rooms_with_sensor_issues": sum(
                        1 for room in occupancy if not room["sensors_available"]
                    ),
                }
            )
            return stats

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "active_level": None,
            "activated_at": None,
            "last_applied_at": None,
            "lockout_enabled": False,
            "released_entities": [],
            "release_details": {},
            "room_occupancy": {},
            "revision": 0,
        }

    def _coerce_state(self, stored: Any) -> dict[str, Any]:
        """Rebuild a valid state document from what was stored.

        Fields that fail validation fall back to their defaults one by one,
        so a damaged record never blocks startup.
        """
        state = self._default_state()
        if not isinstance(stored, dict):
            return state

        active_level = stored.get("active_level")
        if active_level in ENERGY_LEVELS:
            state["active_level"] = active_level
        for field in ("activated_at", "last_applied_at"):
            value = stored.get(field)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value >= 0
            ):
                state[field] = int(value)
        revision = stored.get("revision")
        if (
            isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
        ):
            state["revision"] = revision

        if state["active_level"] is not None:
            if state["active_level"] in (LEVEL_LOW, LEVEL_MEDIUM):
                state["lockout_enabled"] = True
            elif isinstance(stored.get("lockout_enabled"), bool):
                state["lockout_enabled"] = stored["lockout_enabled"]
            else:
                state["lockout_enabled"] = True

            raw_released = stored.get("released_entities")
            try:
                released = _clean_unique_list(
                    raw_released if isinstance(raw_released, list) else [],
                    "released_entities",
                    entity_ids=True,
                )
            except EnergyConfigError:
                released = []
            raw_details = stored.get("release_details")
            if not isinstance(raw_details, dict):
                raw_details = {}
            details: dict[str, dict[str, Any]] = {}
            for entity_id in released:
                raw = raw_details.get(entity_id)
                if not isinstance(raw, dict):
                    raw = {}
                room_id = raw.get("room_id")
                if not isinstance(room_id, str) or not room_id.strip():
                    room_id = None
                released_at = raw.get("released_at")
                if (
                    not isinstance(released_at, (int, float))
                    or isinstance(released_at, bool)
                    or released_at < 0
                ):
                    released_at = 0
                source = raw.get("source")
                if not isinstance(source, str) or not source.strip():
                    source = "unknown"
                details[entity_id] = {
                    "room_id": room_id,
                    "released_at": int(released_at),
                    "source": source[:_MAX_SOURCE_LENGTH],
                }
            state["released_entities"] = sorted(details)
            state["release_details"] = details

            raw_occupancy = stored.get("room_occupancy")
            if state["active_level"] == LEVEL_SMART and isinstance(raw_occupancy, dict):
                for raw_room_id, raw in raw_occupancy.items():
                    if not isinstance(raw, dict):
                        continue
                    try:
                        room_id = _clean_id(raw_room_id, "room_id")
                    except EnergyConfigError:
                        continue
                    available = raw.get("sensors_available")
                    occupied = raw.get("occupied")
                    if not isinstance(available, bool):
                        continue
                    if available and not isinstance(occupied, bool):
                        continue
                    if not available:
                        occupied = None
                    changed_at = raw.get("changed_at")
                    if (
                        not isinstance(changed_at, (int, float))
                        or isinstance(changed_at, bool)
                        or changed_at < 0
                    ):
                        changed_at = 0
                    state["room_occupancy"][room_id] = {
                        "occupied": occupied,
                        "sensors_available": available,
                        "changed_at": int(changed_at),
                    }
        return state

    def _require_active(self) -> str:
        level = self._state["active_level"]
        if level is None:
            raise EnergyInactiveError("Energy Saving is not active")
        return level

    def _next_state(self) -> dict[str, Any]:
        """A copy of the live state with the next revision, to edit and commit."""
        state = copy.deepcopy(self._state)
        state["revision"] += 1
        return state

    def _commit_state(self, state: dict[str, Any]) -> None:
        """Write the state, then make it the live state.

        A write that raises leaves the live state as it was, matching disk.
        """
        self._state_table[_STATE_KEY] = copy.deepcopy(state)
        self._state = state

    def _record_event(
        self,
        kind: str,
        *,
        level: str | None = None,
        entity_id: str | None = None,
        room_id: str | None = None,
        data: dict[str, Any] | None = None,
        t: int | None = None,
    ) -> dict[str, Any]:
        return self._events.append(
            t=self._now() if t is None else t,
            kind=kind,
            level=level,
            entity_id=entity_id,
            room_id=room_id,
            data=data,
        )

    def _now(self) -> int:
        """The injected clock as whole seconds; refuses a nonsense value."""
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise EnergyError("clock must return a finite non-negative number")
        return int(value)

    @staticmethod
    def _optional_actor(actor: Any) -> str | None:
        if actor is None:
            return None
        return _clean_id(actor, "actor")
