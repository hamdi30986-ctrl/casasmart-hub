"""Hub-owned state behind the Now page, and the rules for room commands.

NowDataEngine keeps, each in its own table: each member's recently
controlled devices (recorded after a command succeeds), each room's activity
policy (a room without one takes part with its lights and fans), the Now
configuration, recent room-command answers for idempotent retries, each
room's restore set (what its last off switched off, with each AC's mode and
target) and each room's start profile (the lights and fans an installer chose
for one tap "on" when nothing else is known). The functions decide which
devices may join a room command (room_command_approves: the devices a room's
policy can hold), what makes a room on (room_on_kind: those, and a running
AC), what room off keeps of an AC and how it comes back (ac_capture_level,
ac_restore_steps), and what one tap may turn back on without a capture
(room_light_kind: the room's lights and fans). Storage methods are
synchronous; now_api runs them in the executor.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

_MAX_RECENTS = 48
_MAX_PINNED_MOMENTS = 12
_MAX_CONTACTS = 64
# Stored room-command answers per member; the oldest is dropped first.
_MAX_IDEMPOTENCY_ENTRIES = 64
# Devices in one room's start profile (as in a room policy).
MAX_START_PROFILE = 64
# A room off this soon after the room's capture adds to it (a second tap
# finds little still on); a later one is a new off and starts a new capture.
# As room_intel.one_tap.OFF_SEQUENCE_SECONDS.
_CAPTURE_EXTEND_SECONDS = 120
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# The registry gang types that make a wall relay (a switch) a light or a fan.
# The installer sets them in the app's gang-type picker; Home Assistant has
# no device class for a switch that drives a light. Outlets, heaters and
# untyped switches ("switch", or none) never are.
LIGHT_RELAY_GANG_TYPES = frozenset({"light", "fan"})
# The registry gang types of an untyped wall relay: "switch", or none. Room
# off switches one, as most lights are on a wall relay no one typed, but
# one tap never turns one back on (not a light to it).
UNTYPED_RELAY_GANG_TYPES = frozenset({"switch", None})
# Home Assistant's device class for a switch that is a plug or socket: never
# switched by a room command, whatever its gang type.
_OUTLET_DEVICE_CLASS = "outlet"
# What room_on_kind calls an AC.
KIND_AC = "ac"
# The HVAC modes an AC runs in: every mode but off (generated_suggestions
# room_actions' set).
AC_ON_MODES = frozenset({"cool", "heat", "auto", "heat_cool", "dry", "fan_only"})
# Home Assistant's ClimateEntityFeature.TARGET_TEMPERATURE.
_SUPPORT_TARGET_TEMPERATURE = 1


class NowDataError(Exception):
    """A Now configuration, policy or command value is invalid."""


def _optional_entity_id(value: Any, field: str) -> str | None:
    """An entity-id-shaped string, or None."""
    if value is None:
        return None
    if not isinstance(value, str) or "." not in value or len(value) > 255:
        raise NowDataError(f"{field} must be an entity_id string or null")
    return value


def _timestamp(value: datetime | None = None) -> str:
    """The time (default: now) as an ISO 8601 UTC string."""
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _within(stamp: str | None, at: datetime, seconds: float) -> bool:
    """True when the ISO 8601 stamp is at most seconds before at (and not
    after it); an unreadable one is not."""
    try:
        then = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        return False
    if then is None or then.tzinfo is None:
        return False
    return 0 <= (at - then).total_seconds() <= seconds


def _state_value(state: Any, field: str, default: Any = None) -> Any:
    """Read a field from an HA State or from a plain dict."""
    if isinstance(state, dict):
        return state.get(field, default)
    return getattr(state, field, default)


def _state_attributes(state: Any) -> dict[str, Any]:
    attributes = _state_value(state, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


def lighting_kind(state: Any, gang_type: str | None = None) -> str | None:
    """ "light" or "fan" for a device that is a light or a fan to the hub, else
    None.

    A light or a fan entity, or a wall relay (a switch) whose registry gang
    type the installer set to light or fan (gang_type), unless Home
    Assistant calls it an outlet. Never a plug, outlet, heater, untyped
    switch, AC, cover or TV, nor a device left out of room activity. What
    one tap "on" may turn on (room_light_kind).
    """
    entity_id = _state_value(state, "entity_id", "")
    attributes = _state_attributes(state)
    if attributes.get("casasmart_room_activity_exclude") is True:
        return None
    domain = entity_id.split(".", 1)[0]
    if domain in ("light", "fan"):
        return domain
    if (
        domain == "switch"
        and gang_type in LIGHT_RELAY_GANG_TYPES
        and attributes.get("device_class") != _OUTLET_DEVICE_CLASS
    ):
        return gang_type
    return None


def room_off_kind(state: Any, gang_type: str | None = None) -> str | None:
    """ "light", "fan" or "switch" for a device room off may switch, else None.

    A light, a fan or a wall relay typed as one (lighting_kind), or an
    untyped wall relay: a switch whose registry gang type (gang_type) is
    "switch" or none (most lights are on a wall relay no one typed). Never
    a switch Home Assistant calls an outlet, one typed as
    an outlet, plug or heater, an AC, a cover, a TV, nor a device left out of
    room activity. While on, each of these makes its room on (room_on_kind).
    Room off also switches the room's running ACs, which no policy lists
    (is_room_ac).
    """
    kind = lighting_kind(state, gang_type)
    if kind is not None:
        return kind
    entity_id = _state_value(state, "entity_id", "")
    attributes = _state_attributes(state)
    if (
        entity_id.split(".", 1)[0] == "switch"
        and gang_type in UNTYPED_RELAY_GANG_TYPES
        and attributes.get("device_class") != _OUTLET_DEVICE_CLASS
        and attributes.get("casasmart_room_activity_exclude") is not True
    ):
        return "switch"
    return None


def is_room_ac(state: Any) -> bool:
    """True for an AC: a cooling-capable climate entity not left out of room
    activity (generated_suggestions.room_actions' test for an AC). Room off
    switches one off while it runs, in any room that takes part, whatever its
    policy lists; casasmart_room_activity_exclude is how a device stays out."""
    attributes = _state_attributes(state)
    return (
        _state_value(state, "entity_id", "").startswith("climate.")
        and "cool" in (attributes.get("hvac_modes") or [])
        and attributes.get("casasmart_room_activity_exclude") is not True
    )


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def ac_capture_level(state: Any) -> dict[str, Any] | None:
    """What room off keeps of a running AC (is_room_ac), else None.

    Its hvac_mode and, when it has one it takes, its target temperature. Some
    ACs report no target in dry or fan mode: the mode alone is what they were.
    """
    if not is_room_ac(state):
        return None
    mode = _state_value(state, "state")
    if mode not in AC_ON_MODES:
        return None
    attributes = _state_attributes(state)
    target = attributes.get("temperature")
    if int(
        attributes.get("supported_features") or 0
    ) & _SUPPORT_TARGET_TEMPERATURE and _is_number(target):
        return {"hvac_mode": mode, "temperature": target}
    return {"hvac_mode": mode}


def ac_restore_steps(state: Any, level: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The scene steps that bring an AC back to its captured level: its mode,
    then its target when it had one.

    Never a fan step: an IR AC takes every climate call as a full-state
    burst. None when it is no longer an AC of the room's (is_room_ac), or no
    longer takes that mode or target: an AC never comes on at a guess.
    """
    attributes = _state_attributes(state)
    mode = level.get("hvac_mode")
    if (
        not is_room_ac(state)
        or mode not in AC_ON_MODES
        or mode not in attributes["hvac_modes"]
    ):
        return []
    entity_id = _state_value(state, "entity_id", "")
    steps = [
        {
            "entity_id": entity_id,
            "action": "set_hvac_mode",
            "data": {"hvac_mode": mode},
        }
    ]
    target = level.get("temperature")
    if target is None:
        return steps
    minimum, maximum = attributes.get("min_temp"), attributes.get("max_temp")
    if not (
        int(attributes.get("supported_features") or 0) & _SUPPORT_TARGET_TEMPERATURE
        and _is_number(target)
        and (not _is_number(minimum) or minimum <= target)
        and (not _is_number(maximum) or target <= maximum)
    ):
        return []
    steps.append(
        {
            "entity_id": entity_id,
            "action": "set_temperature",
            "data": {"temperature": target},
        }
    )
    return steps


def is_room_activity_candidate(state: Any, gang_type: str | None = None) -> bool:
    """True when an admin may approve this device for room commands: one
    room off may switch (room_off_kind).

    gang_type is the device's registry gang type (None: unknown or untyped).
    """
    return room_off_kind(state, gang_type) is not None


def is_room_activity_eligible(
    state: Any, eligible_entity_ids: Iterable[str], gang_type: str | None = None
) -> bool:
    """True for a candidate device that the room's stored policy approves."""
    entity_id = _state_value(state, "entity_id", "")
    return entity_id in frozenset(eligible_entity_ids) and is_room_activity_candidate(
        state, gang_type
    )


def is_default_room_device(state: Any, gang_type: str | None = None) -> bool:
    """True for a device a room without a stored policy switches: a light, a
    fan, or a wall relay typed as one or untyped (room_off_kind; gang_type
    is its registry gang type).

    As in Apple Home and Google Home, a room's lights act together with no
    setup. Never a plug, outlet, heater, AC, cover or TV. The caller also
    checks the device is imported into the registry, served and in the room.
    """
    return room_off_kind(state, gang_type) is not None


def default_room_policy() -> dict[str, Any]:
    """The policy of a room without a stored one (room_policy): it takes part
    with its default devices (is_default_room_device)."""
    return {"participates": True, "eligible_entity_ids": [], "default": True}


def room_command_approves(
    state: Any, policy: Mapping[str, Any], gang_types: Mapping[str, str | None]
) -> bool:
    """True when room commands in a room with this policy switch the device.

    The one rule for the devices a policy decides. A stored policy approves
    the candidates on its list. A room without one (policy default) takes
    part with its default devices: its lights, fans and wall relays (typed
    as a light or fan, or untyped) imported into the registry
    (is_default_room_device). Either way only devices room_off_kind allows,
    so never a plug, outlet or heater, and never an AC: no policy lists one,
    and room off takes the room's running ACs by is_room_ac. gang_types are
    the registry's imported controls with their gang types
    (ranking.imported_controls). The caller checks the device is in the room
    and served, and that the room takes part.
    """
    entity_id = _state_value(state, "entity_id", "")
    if policy.get("default"):
        return entity_id in gang_types and is_default_room_device(
            state, gang_types[entity_id]
        )
    return is_room_activity_eligible(
        state, policy.get("eligible_entity_ids") or [], gang_types.get(entity_id)
    )


def room_light_kind(
    state: Any, policy: Mapping[str, Any], gang_types: Mapping[str, str | None]
) -> str | None:
    """ "light" or "fan" for a light or fan (lighting_kind) that room commands
    switch in a room with this policy (room_command_approves), else None.

    What one tap "on" may turn back on. Never an untyped wall relay (room off
    switches one, one tap doesn't turn it back on), a plug, an outlet, a
    heater, an AC, a cover or a TV.
    """
    if not room_command_approves(state, policy, gang_types):
        return None
    return lighting_kind(state, gang_types.get(_state_value(state, "entity_id", "")))


def room_on_kind(
    state: Any, policy: Mapping[str, Any], gang_types: Mapping[str, str | None]
) -> str | None:
    """What a device is to its room's on state, in a room with this policy;
    None for a device that never makes it on.

    "light", "fan" or "switch" for a device room commands switch there
    (room_command_approves, room_off_kind), so room off leaves none of them
    on. KIND_AC for an AC (is_room_ac) in any room, whatever its policy:
    a running AC keeps its room on until room off switches it too. Never
    a plug, outlet, heater, cover or TV. The one rule for
    the Now page's room cards (room_intel.ranking: active rooms, the
    all-off tiers, the habits). An AC is on in AC_ON_MODES, anything else
    when "on".
    """
    if is_room_ac(state):
        return KIND_AC
    if not room_command_approves(state, policy, gang_types):
        return None
    return room_off_kind(state, gang_types.get(_state_value(state, "entity_id", "")))


def summarize_openings(
    entity_states: Iterable[tuple[str, str | None]],
) -> dict[str, str | int]:
    """Count the open and unknown doors, windows and locks.

    A binary sensor is open when on and closed when off; a lock is open when
    unlocked and closed when locked. Any other state counts as unknown.
    """
    opened = 0
    unknown = 0
    for entity_id, state in entity_states:
        if entity_id.startswith("binary_sensor."):
            if state == "on":
                opened += 1
            elif state != "off":
                unknown += 1
        elif entity_id.startswith("lock."):
            if state == "unlocked":
                opened += 1
            elif state != "locked":
                unknown += 1
        else:
            unknown += 1
    return {
        "status": "open" if opened else ("unknown" if unknown else "all_closed"),
        "open_count": opened,
        "unknown_count": unknown,
    }


def room_activity_layout(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    """Lay out the room cards for the Now page.

    Up to four rooms are plain cards. With more, the first is featured, the
    next four are cards and the rest are counted for "view all".
    """
    count = len(rooms)
    if count == 0:
        return {"featured_room_id": None, "cards": [], "view_all_count": 0}
    if count <= 4:
        return {
            "featured_room_id": None,
            "cards": [room["room_id"] for room in rooms],
            "view_all_count": 0,
        }
    return {
        "featured_room_id": rooms[0]["room_id"],
        "cards": [room["room_id"] for room in rooms[1:5]],
        "view_all_count": count - 5,
    }


class NowDataEngine:
    """Persistent Now state, one key-value table per kind.

    An RLock keeps each read-modify-write whole.
    """

    def __init__(
        self,
        recents_table: Any,
        policies_table: Any,
        config_table: Any,
        restores_table: Any,
        idempotency_table: Any,
        start_profiles_table: Any = None,
    ) -> None:
        self._recents = recents_table
        self._policies = policies_table
        self._config = config_table
        self._restores = restores_table
        self._idempotency = idempotency_table
        # The hub passes its kv table; a test may leave it in memory.
        self._start_profiles = (
            start_profiles_table if start_profiles_table is not None else {}
        )
        self._lock = threading.RLock()

    def record_successful_control(
        self, member_id: str, entity_id: str, at: datetime | None = None
    ) -> None:
        """Record a device the member controlled, newest first."""
        if not member_id or not isinstance(entity_id, str) or "." not in entity_id:
            return
        with self._lock:
            current = self._recents.get(member_id) or {}
            items = current.get("items", []) if isinstance(current, dict) else []
            cleaned = [
                item
                for item in items
                if isinstance(item, dict) and item.get("entity_id") != entity_id
            ]
            cleaned.insert(0, {"entity_id": entity_id, "at": _timestamp(at)})
            self._recents[member_id] = {"items": cleaned[:_MAX_RECENTS]}

    def forget_member(self, member_id: str) -> None:
        """Delete the member's recent controls and stored room-command
        answers (their last device left)."""
        with self._lock:
            self._recents.pop(member_id, None)
            self._idempotency.pop(member_id, None)

    def recents_for(self, member_id: str) -> list[dict[str, str]]:
        """The member's recent controls, newest first."""
        record = self._recents.get(member_id) or {}
        items = record.get("items", []) if isinstance(record, dict) else []
        return [
            {"entity_id": item["entity_id"], "at": item["at"]}
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("entity_id"), str)
            and isinstance(item.get("at"), str)
        ]

    def set_room_policy(
        self, room_id: str, participates: Any, eligible_entity_ids: Any
    ) -> dict[str, Any]:
        """Store a room's activity policy and return it.

        now_api has already checked that every entity is a candidate in this
        room.
        """
        if not isinstance(participates, bool):
            raise NowDataError("participates must be a boolean")
        if not isinstance(eligible_entity_ids, list) or any(
            not isinstance(entity_id, str) or "." not in entity_id
            for entity_id in eligible_entity_ids
        ):
            raise NowDataError("eligible_entity_ids must be a list of entity ids")
        with self._lock:
            policy = {
                "participates": participates,
                "eligible_entity_ids": list(dict.fromkeys(eligible_entity_ids)),
            }
            self._policies[room_id] = policy
        return {"room_id": room_id, **policy}

    def room_policy(self, room_id: str) -> dict[str, Any]:
        """The room's policy: {participates, eligible_entity_ids, default}.

        A room without a valid stored policy takes part with its default
        devices (default: True, eligible_entity_ids empty): the caller derives
        them with is_default_room_device. A stored policy always wins, so an
        admin can take a room out (participates false) or pick its devices.
        """
        record = self._policies.get(room_id)
        if not isinstance(record, dict):
            return default_room_policy()
        entity_ids = record.get("eligible_entity_ids")
        return {
            "participates": bool(record.get("participates") is True),
            "eligible_entity_ids": [
                entity_id for entity_id in entity_ids if isinstance(entity_id, str)
            ]
            if isinstance(entity_ids, list)
            else [],
            "default": False,
        }

    def room_participates(self, room_id: str) -> bool:
        """True when the room takes part in room commands."""
        return self.room_policy(room_id)["participates"]

    def configure(self, payload: Any) -> dict[str, Any]:
        """Merge the fields present in the payload into the Now config.

        Checks shapes only; now_api checks that the entities and scenes exist
        and are of the right kind. Returns the whole config.
        """
        if not isinstance(payload, dict):
            raise NowDataError("Body must be a JSON object")
        allowed = {
            "outdoor_weather_entity_id",
            "air_quality_entity_id",
            "contact_entity_ids",
            "suggested_scene_id",
            "pinned_scene_ids",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise NowDataError(
                f"Unknown Now configuration field: {sorted(unknown)[0]!r}"
            )
        with self._lock:
            record = self._config.get("global") or {}
            if "outdoor_weather_entity_id" in payload:
                record["outdoor_weather_entity_id"] = _optional_entity_id(
                    payload["outdoor_weather_entity_id"], "outdoor_weather_entity_id"
                )
            if "air_quality_entity_id" in payload:
                record["air_quality_entity_id"] = _optional_entity_id(
                    payload["air_quality_entity_id"], "air_quality_entity_id"
                )
            if "suggested_scene_id" in payload:
                raw = payload["suggested_scene_id"]
                if raw is not None and (not isinstance(raw, str) or not raw):
                    raise NowDataError("suggested_scene_id must be a string or null")
                record["suggested_scene_id"] = raw
            if "contact_entity_ids" in payload:
                raw = payload["contact_entity_ids"]
                if not isinstance(raw, list) or len(raw) > _MAX_CONTACTS:
                    raise NowDataError(
                        f"contact_entity_ids must contain at most {_MAX_CONTACTS} entity ids"
                    )
                if any(
                    not isinstance(item, str)
                    or not item.startswith(("binary_sensor.", "lock."))
                    for item in raw
                ):
                    raise NowDataError(
                        "contact_entity_ids must be binary_sensor or lock entity ids"
                    )
                record["contact_entity_ids"] = list(dict.fromkeys(raw))
            if "pinned_scene_ids" in payload:
                raw = payload["pinned_scene_ids"]
                if not isinstance(raw, list) or len(raw) > _MAX_PINNED_MOMENTS:
                    raise NowDataError(
                        f"pinned_scene_ids must contain at most {_MAX_PINNED_MOMENTS} scene ids"
                    )
                if any(not isinstance(item, str) or not item for item in raw):
                    raise NowDataError("pinned_scene_ids must be scene id strings")
                record["pinned_scene_ids"] = list(dict.fromkeys(raw))
            self._config["global"] = record
        return self.config()

    def contacts_picked(self) -> bool:
        """True once a contact list was saved, even an empty one."""
        return "contact_entity_ids" in (self._config.get("global") or {})

    def config(self) -> dict[str, Any]:
        """The Now config, with every field present."""
        raw = self._config.get("global") or {}
        return {
            "outdoor_weather_entity_id": raw.get("outdoor_weather_entity_id"),
            "air_quality_entity_id": raw.get("air_quality_entity_id"),
            "contact_entity_ids": list(raw.get("contact_entity_ids") or []),
            "suggested_scene_id": raw.get("suggested_scene_id"),
            "pinned_scene_ids": list(raw.get("pinned_scene_ids") or []),
        }

    @staticmethod
    def validate_idempotency_key(value: Any) -> str:
        """Return the client's idempotency key, or raise if it is malformed."""
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise NowDataError("idempotency_key must be 8-128 URL-safe characters")
        return value

    def idempotent_result(
        self, member_id: str, room_id: str, action: str, key: str
    ) -> dict[str, Any] | None:
        """The stored answer for this member's (room, action, key), if any."""
        record = self._idempotency.get(member_id) or {}
        result = (record.get("results") or {}).get(f"{room_id}:{action}:{key}")
        return dict(result) if isinstance(result, dict) else None

    def save_idempotent_result(
        self,
        member_id: str,
        room_id: str,
        action: str,
        key: str,
        result: dict[str, Any],
    ) -> None:
        """Store a room command's answer so a retry replays it."""
        with self._lock:
            record = self._idempotency.get(member_id) or {}
            results = (
                record.get("results") if isinstance(record.get("results"), dict) else {}
            )
            results[f"{room_id}:{action}:{key}"] = result
            # Keep the newest entries; JSON keeps insertion order.
            while len(results) > _MAX_IDEMPOTENCY_ENTRIES:
                results.pop(next(iter(results)))
            self._idempotency[member_id] = {"results": results}

    def restore_set(self, room_id: str) -> list[str]:
        """What the room's last off switched off and on has not yet restored,
        its ACs apart (restore_acs)."""
        return self.restore_capture(room_id)[0]

    def restore_capture(self, room_id: str) -> tuple[list[str], str | None]:
        """The room's restore set and when it was first captured (ISO 8601 UTC,
        None if unknown)."""
        record = self._restores.get(room_id) or {}
        if not isinstance(record, dict):
            return [], None
        entities = record.get("entity_ids") or []
        captured_at = record.get("captured_at")
        return (
            [item for item in entities if isinstance(item, str)]
            if isinstance(entities, list)
            else [],
            captured_at if isinstance(captured_at, str) else None,
        )

    def restore_acs(self, room_id: str) -> dict[str, dict[str, Any]]:
        """The ACs in the room's capture, each with its level (ac_capture_level)."""
        record = self._restores.get(room_id)
        acs = record.get("acs") if isinstance(record, dict) else None
        if not isinstance(acs, dict):
            return {}
        return {
            entity_id: level
            for entity_id, level in acs.items()
            if isinstance(entity_id, str) and isinstance(level, dict)
        }

    def save_restore_set(self, room_id: str, entity_ids: Iterable[str]) -> None:
        """Replace the room's restore set; an empty set removes it."""
        ids = list(dict.fromkeys(entity_ids))
        with self._lock:
            if ids:
                self._restores[room_id] = {
                    "entity_ids": ids,
                    "captured_at": _timestamp(),
                }
            else:
                self._restores.pop(room_id, None)

    def extend_restore_set(
        self,
        room_id: str,
        entity_ids: Iterable[str],
        at: datetime | None = None,
        acs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[str]:
        """Take what a room off just switched off (at: now) and return
        everything in the room's capture: its restore set, then its ACs.

        acs are the ACs it switched off, each with its level
        (ac_capture_level). An off that switched nothing leaves the capture as
        it is. One within _CAPTURE_EXTEND_SECONDS of the capture adds to it
        (ids keep their first-captured order, each once; an AC captured again
        takes its newer level) and keeps when it was captured, so a repeated
        tap never makes an old capture new. A later one is what the room had
        on last time: it replaces the capture.
        """
        ids = list(dict.fromkeys(entity_ids))
        levels = dict(acs or {})
        at = at or datetime.now(UTC)
        with self._lock:
            current, captured_at = self.restore_capture(room_id)
            current_acs = self.restore_acs(room_id)
            if not ids and not levels:
                return [*current, *current_acs]
            if (current or current_acs) and _within(
                captured_at, at, _CAPTURE_EXTEND_SECONDS
            ):
                ids = list(dict.fromkeys([*current, *ids]))
                levels = {**current_acs, **levels}
            else:
                captured_at = _timestamp(at)
            record = {"entity_ids": ids, "captured_at": captured_at}
            if levels:
                record["acs"] = levels
            self._restores[room_id] = record
            return [*ids, *levels]

    def keep_in_restore_set(self, room_id: str, entity_ids: Iterable[str]) -> list[str]:
        """Keep only these ids in the room's capture (its restore set and its
        ACs) and return what is left.

        They keep their captured order and levels, and the capture its
        captured_at; an empty result removes the set.
        """
        keep = set(entity_ids)
        with self._lock:
            record = self._restores.get(room_id)
            ids = [item for item in self.restore_set(room_id) if item in keep]
            acs = {
                entity_id: level
                for entity_id, level in self.restore_acs(room_id).items()
                if entity_id in keep
            }
            if ids or acs:
                captured_at = (
                    record.get("captured_at") if isinstance(record, dict) else None
                )
                kept = {
                    "entity_ids": ids,
                    "captured_at": captured_at or _timestamp(),
                }
                if acs:
                    kept["acs"] = acs
                self._restores[room_id] = kept
            else:
                self._restores.pop(room_id, None)
            return [*ids, *acs]

    def consume_restore_set(self, room_id: str) -> list[str]:
        """Return the room's restore set and clear it."""
        with self._lock:
            ids = self.restore_set(room_id)
            self._restores.pop(room_id, None)
            return ids

    def start_profile(self, room_id: str) -> list[str]:
        """The room's start profile, [] when it has none."""
        record = self._start_profiles.get(room_id)
        ids = record.get("entity_ids") if isinstance(record, dict) else None
        if not isinstance(ids, list):
            return []
        return [item for item in ids if isinstance(item, str)]

    def set_start_profile(self, room_id: str, entity_ids: Any) -> dict[str, Any]:
        """Store the room's start profile and return it.

        Checks the shape only (1 to MAX_START_PROFILE distinct entity ids);
        now_api has already checked each is a light or fan in this room.
        """
        if (
            not isinstance(entity_ids, list)
            or not 0 < len(entity_ids) <= MAX_START_PROFILE
            or any(
                not isinstance(entity_id, str) or "." not in entity_id
                for entity_id in entity_ids
            )
        ):
            raise NowDataError(
                f"entity_ids must list 1 to {MAX_START_PROFILE} entity ids"
            )
        if len(set(entity_ids)) != len(entity_ids):
            raise NowDataError("entity_ids must not contain duplicates")
        with self._lock:
            self._start_profiles[room_id] = {"entity_ids": list(entity_ids)}
        return {"room_id": room_id, "entity_ids": list(entity_ids)}

    def clear_start_profile(self, room_id: str) -> dict[str, Any]:
        """Delete the room's start profile (none is fine) and return it empty."""
        with self._lock:
            self._start_profiles.pop(room_id, None)
        return {"room_id": room_id, "entity_ids": []}

    def forget_room(self, room_id: str) -> None:
        """Delete a deleted room's activity policy, restore set and start
        profile."""
        with self._lock:
            self._policies.pop(room_id, None)
            self._restores.pop(room_id, None)
            self._start_profiles.pop(room_id, None)
