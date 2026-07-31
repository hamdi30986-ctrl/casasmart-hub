"""Home Assistant adapter for CasaSmart Energy Saving (P2).

``energy.py`` owns durable configuration and state.  This module owns the
live Home Assistant side of the contract:

* deterministic activation/re-apply rules for gangs, climate, lights, plugs,
  heaters, and covers;
* Smart room occupancy with instant welcome, a cancellable 45-second empty
  grace, and the boost-then-settle climate state machine;
* Medium temperature guards and Smart boost temperature edges;
* the shared sun clock used by covers and dark-only welcome lighting; and
* an own-command ledger so a human state change releases one device instead
  of making the mode fight them.

P2 deliberately does not register APIs or wire the adapter into integration
setup.  P3 owns that lifecycle and lockout boundary.  Keeping this adapter
constructible in isolation makes the full ruleset testable without a live
Home Assistant installation.
"""

from __future__ import annotations

import copy
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .energy import (
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_SMART,
    EnergyEngine,
)
from .registry import UNSET, RegistryEngine

_LOGGER = logging.getLogger(__name__)

EMPTY_GRACE_SECONDS = 45
BOOST_FAILSAFE_SECONDS = 15 * 60
OWN_COMMAND_GRACE_SECONDS = 15
COVER_COMMAND_GRACE_SECONDS = 120

COOL_FLOOR = 24.0
COOL_KILL_BELOW = 22.0
COOL_BOOST_ABOVE = 28.0
COOL_BOOST_TARGET = 16.0

HEAT_CEILING = 21.0
HEAT_KILL_ABOVE = 24.0
HEAT_BOOST_BELOW = 18.0
HEAT_BOOST_TARGET = 30.0

WELCOME_BRIGHTNESS_PCT = 60
LIGHT_CAP_PCT = {
    LEVEL_LOW: 80,
    LEVEL_MEDIUM: 60,
    LEVEL_SMART: 40,
}

_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
_PRESENCE_CLASSES = frozenset({"occupancy", "motion", "presence"})
_MANAGED_DOMAINS = frozenset({"light", "switch", "climate", "cover"})

EVENT_RULES_APPLIED = "rules_applied"
EVENT_RULE_FAILED = "rule_failed"
EVENT_ROOM_SKIPPED = "room_skipped"
EVENT_BOOST_STARTED = "boost_started"
EVENT_BOOST_SETTLED = "boost_settled"
EVENT_ENERGY_POSTURE = "energy_posture"
EVENT_COMFORT_POSTURE = "comfort_posture"


def _domain(entity_id: str) -> str:
    return entity_id.partition(".")[0]


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _state_changed(old_state: Any, new_state: Any) -> bool:
    if old_state is None or new_state is None:
        return old_state is not new_state
    return (
        old_state.state != new_state.state
        or dict(getattr(old_state, "attributes", {}) or {})
        != dict(getattr(new_state, "attributes", {}) or {})
    )


@dataclass(frozen=True)
class EnergyEntity:
    """Small immutable state snapshot used by the rules."""

    entity_id: str
    state: str
    attributes: dict[str, Any]
    room_id: str | None
    last_changed: datetime | None = None
    entity_category: str | None = None

    @property
    def available(self) -> bool:
        return self.state not in _UNAVAILABLE_STATES

    @property
    def on(self) -> bool:
        return self.state == "on"

    @property
    def brightness(self) -> int | None:
        value = self.attributes.get("brightness")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return max(0, min(255, int(value)))

    @property
    def temperature(self) -> float | None:
        return _as_float(self.state)


@dataclass(frozen=True)
class GangGroup:
    """One physical two/three-gang wall switch."""

    group_id: str
    room_id: str | None
    entity_ids: tuple[str, ...]


@dataclass
class RoomInventory:
    """Entities participating in Energy Saving for one room."""

    room_id: str
    climates: list[EnergyEntity] = field(default_factory=list)
    lights: list[EnergyEntity] = field(default_factory=list)
    covers: list[EnergyEntity] = field(default_factory=list)
    temperature_sensors: list[EnergyEntity] = field(default_factory=list)
    presence_sensors: list[EnergyEntity] = field(default_factory=list)

    @property
    def automatic(self) -> bool:
        """Smart v1 requires both temperature and presence."""
        return bool(self.temperature_sensors and self.presence_sensors)

    @property
    def sensor_entities(self) -> tuple[EnergyEntity, ...]:
        return tuple(self.temperature_sensors + self.presence_sensors)

    @property
    def sensors_available(self) -> bool:
        return all(entity.available for entity in self.sensor_entities)

    @property
    def room_temperature(self) -> float | None:
        values = [
            entity.temperature
            for entity in self.temperature_sensors
            if entity.available and entity.temperature is not None
        ]
        return values[0] if values else None

    @property
    def occupied(self) -> bool | None:
        if not self.presence_sensors or not self.sensors_available:
            return None
        return any(entity.state == "on" for entity in self.presence_sensors)


@dataclass(frozen=True)
class EnergyInventory:
    """A point-in-time view of the HA entity graph."""

    entities: dict[str, EnergyEntity]
    rooms: dict[str, RoomInventory]
    gangs: tuple[GangGroup, ...]
    wall_control_ids: frozenset[str]


@dataclass(frozen=True)
class SunContext:
    day: bool
    heat_window: bool
    next_heat_window_start: datetime | None


@dataclass
class _Boost:
    room_id: str
    mode: str
    entity_ids: set[str]
    cancel_timer: Callable[[], None]


class EnergyInventoryBuilder:
    """Build Energy Saving inventory from HA state + CasaSmart registry.

    Registry assignments win.  If a record is absent, the default resolver
    asks HA's entity/device area registry.  Tests may inject a trivial resolver
    and avoid importing the HA registries entirely.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        registry: RegistryEngine,
        *,
        area_resolver: Callable[[HomeAssistant, str], str | None] | None = None,
        category_resolver: (
            Callable[[HomeAssistant, str], str | None] | None
        ) = None,
    ) -> None:
        self._hass = hass
        self._registry = registry
        self._area_resolver = area_resolver
        self._category_resolver = category_resolver

    def _room_of(self, entity_id: str) -> str | None:
        room_id = self._registry.room_of(entity_id)
        if room_id is not UNSET:
            return room_id
        if self._area_resolver is not None:
            return self._area_resolver(self._hass, entity_id)
        try:
            # Lazy import: the adapter's unit suite intentionally has no HA
            # registry modules, while the live integration does.
            from .filtering import ha_area_id_of

            return ha_area_id_of(self._hass, entity_id)
        except (ImportError, AttributeError):
            return None

    def _category_of(self, entity_id: str) -> str | None:
        if self._category_resolver is not None:
            return self._category_resolver(self._hass, entity_id)
        try:
            from homeassistant.helpers import entity_registry as er

            entry = er.async_get(self._hass).async_get(entity_id)
        except (ImportError, AttributeError):
            return None
        if entry is None or entry.entity_category is None:
            return None
        return str(
            getattr(entry.entity_category, "value", entry.entity_category)
        )

    async def async_build(self) -> EnergyInventory:
        raw_user_devices = await self._hass.async_add_executor_job(
            self._registry.list_user_devices
        )
        raw_states = list(self._hass.states.async_all())
        gang_devices = [
            device
            for device in raw_user_devices
            if self._is_gang_device(device)
        ]
        wall_control_ids = frozenset(
            entity_id
            for device in gang_devices
            for entity_id in device.get("control_entity_ids", [])
            if isinstance(entity_id, str)
        )

        entities: dict[str, EnergyEntity] = {}
        rooms: dict[str, RoomInventory] = {}
        for state in raw_states:
            entity_id = state.entity_id
            domain = _domain(entity_id)
            if domain not in {
                "light",
                "switch",
                "climate",
                "cover",
                "sensor",
                "binary_sensor",
                "sun",
            }:
                continue
            room_id = self._room_of(entity_id)
            entity = EnergyEntity(
                entity_id=entity_id,
                state=str(state.state),
                attributes=copy.deepcopy(
                    dict(getattr(state, "attributes", {}) or {})
                ),
                room_id=room_id,
                last_changed=_as_datetime(getattr(state, "last_changed", None)),
                entity_category=self._category_of(entity_id),
            )
            entities[entity_id] = entity
            if room_id is None:
                continue
            room = rooms.setdefault(room_id, RoomInventory(room_id))
            if domain == "climate":
                room.climates.append(entity)
            elif domain == "light" and entity_id not in wall_control_ids:
                room.lights.append(entity)
            elif domain == "cover":
                room.covers.append(entity)
            elif domain == "sensor" and self._is_temperature(entity):
                room.temperature_sensors.append(entity)
            elif domain == "binary_sensor" and self._is_presence(entity):
                room.presence_sensors.append(entity)

        for room in rooms.values():
            room.climates.sort(key=lambda item: item.entity_id)
            room.lights.sort(key=lambda item: item.entity_id)
            room.covers.sort(key=lambda item: item.entity_id)
            room.temperature_sensors.sort(key=lambda item: item.entity_id)
            room.presence_sensors.sort(key=lambda item: item.entity_id)

        gangs: list[GangGroup] = []
        for device in gang_devices:
            controls = tuple(
                entity_id
                for entity_id in device.get("control_entity_ids", [])
                if entity_id in entities
            )
            if not controls:
                continue
            room_ids = {
                entities[entity_id].room_id
                for entity_id in controls
                if entities[entity_id].room_id is not None
            }
            declared_room = device.get("room_id")
            room_id = (
                declared_room
                if isinstance(declared_room, str)
                else next(iter(room_ids), None)
            )
            gangs.append(
                GangGroup(
                    group_id=str(device.get("ha_device_id") or controls[0]),
                    room_id=room_id,
                    entity_ids=controls,
                )
            )
        gangs.sort(key=lambda group: group.group_id)
        return EnergyInventory(
            entities=entities,
            rooms=rooms,
            gangs=tuple(gangs),
            wall_control_ids=wall_control_ids,
        )

    @staticmethod
    def _is_gang_device(device: dict[str, Any]) -> bool:
        """Registry records cover every device; gang metadata is the divider."""
        gangs = device.get("gangs")
        if isinstance(gangs, dict) and any(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in gangs.items()
        ):
            return True
        legacy_types = device.get("gang_types")
        return isinstance(legacy_types, dict) and bool(legacy_types)

    @staticmethod
    def _is_temperature(entity: EnergyEntity) -> bool:
        if entity.entity_category in {"config", "diagnostic"}:
            return False
        device_class = str(entity.attributes.get("device_class", "")).lower()
        object_id = entity.entity_id.partition(".")[2].lower()
        if object_id.endswith("_device_temperature"):
            return False
        if device_class:
            return device_class == "temperature"
        return (
            object_id in {"temp", "temperature"}
            or object_id.endswith(("_temp", "_temperature"))
        )

    @staticmethod
    def _is_presence(entity: EnergyEntity) -> bool:
        if entity.entity_category in {"config", "diagnostic"}:
            return False
        device_class = str(entity.attributes.get("device_class", "")).lower()
        object_id = entity.entity_id.partition(".")[2].lower()
        return device_class in _PRESENCE_CLASSES or (
            not device_class
            and (
                object_id.endswith(("_presence", "_occupancy", "_motion"))
                or "mmwave" in object_id
            )
        )


class EnergyAdapter:
    """Execute the Energy Saving contract against live HA."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: EnergyEngine,
        registry: RegistryEngine,
        *,
        area_resolver: Callable[[HomeAssistant, str], str | None] | None = None,
        category_resolver: (
            Callable[[HomeAssistant, str], str | None] | None
        ) = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        change_callback: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._engine = engine
        self._builder = EnergyInventoryBuilder(
            hass,
            registry,
            area_resolver=area_resolver,
            category_resolver=category_resolver,
        )
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._change_callback = change_callback

        self._unsub_state_changed: Callable[[], None] | None = None
        self._cancel_sun_timer: Callable[[], None] | None = None
        self._empty_timers: dict[str, Callable[[], None]] = {}
        self._boosts: dict[str, _Boost] = {}

        self._inventory: EnergyInventory | None = None
        self._config: dict[str, Any] | None = None
        self._managed_entities: set[str] = set()
        self._sensor_rooms: dict[str, tuple[str, str]] = {}
        self._entity_rooms: dict[str, str | None] = {}
        self._own_commands: dict[str, float] = {}
        self._issues: dict[tuple[str | None, str, str | None], dict[str, Any]] = {}
        self._command_count = 0
        self._failure_count = 0

    # -- lifecycle ---------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe once; P3 calls ``async_apply`` after engine activation."""
        if self._unsub_state_changed is not None:
            return
        self._unsub_state_changed = self._hass.bus.async_listen(
            "state_changed", self._on_state_changed
        )

    @callback
    def async_stop(self) -> None:
        """Cancel every listener/timer.  Safe to call repeatedly."""
        if self._unsub_state_changed is not None:
            self._unsub_state_changed()
            self._unsub_state_changed = None
        self._cancel_all_timers()
        self._own_commands.clear()
        self._managed_entities.clear()
        self._sensor_rooms.clear()

    @callback
    def async_mode_stopped(self) -> None:
        """Drop dynamic work after P3 deactivates the engine."""
        self._cancel_all_timers()
        self._own_commands.clear()
        self._managed_entities.clear()
        self._sensor_rooms.clear()
        self._inventory = None
        self._config = None

    def issues(self) -> list[dict[str, Any]]:
        """Return current fail-safe warnings for P3's state endpoint."""
        return sorted(
            (copy.deepcopy(issue) for issue in self._issues.values()),
            key=lambda item: (
                item.get("room_id") or "",
                item["code"],
                item.get("entity_id") or "",
            ),
        )

    def manages(self, entity_id: str) -> bool:
        """Whether the active adapter context owns this device."""
        return entity_id in self._managed_entities

    def _notify_changed(self) -> None:
        if self._change_callback is not None:
            self._change_callback()

    # -- activation/re-apply ---------------------------------------------

    async def async_apply(self, *, reason: str = "activation") -> dict[str, Any]:
        """Apply the active level and arm its dynamic rules.

        The engine must already be active.  P3 owns the transaction ordering:
        ``engine.activate/reapply`` first, then this method.  A failed device
        command is isolated and reported; it never aborts the remaining home.
        """
        level = self._engine.active_level
        if level is None:
            return {"level": None, "commands": 0, "failures": 0, "issues": []}

        self._cancel_all_timers()
        self._issues.clear()
        self._command_count = 0
        self._failure_count = 0
        inventory = await self._builder.async_build()
        config = await self._hass.async_add_executor_job(
            self._engine.get_config, level
        )
        self._set_context(inventory, config)
        excluded = set(config["excluded_rooms"])
        troubled = self._troubled_rooms(inventory)

        await self._apply_gangs(level, inventory, config, excluded, troubled)
        await self._apply_common_offs(inventory, config, excluded, troubled)

        for room_id, room in inventory.rooms.items():
            if room_id in excluded or room_id in troubled:
                continue
            if level == LEVEL_SMART and room.automatic:
                await self._initialize_smart_room(room)
            else:
                await self._apply_static_climate(level, room, config)
                await self._apply_static_lights(level, room, config)
                await self._apply_static_covers(level, room)

        self._schedule_sun_timer()
        summary = {
            "level": level,
            "reason": reason,
            "commands": self._command_count,
            "failures": self._failure_count,
            "issues": self.issues(),
        }
        await self._record_event(
            EVENT_RULES_APPLIED,
            level=level,
            data={
                "reason": reason,
                "commands": self._command_count,
                "failures": self._failure_count,
                "issue_count": len(summary["issues"]),
            },
        )
        return summary

    def _set_context(
        self, inventory: EnergyInventory, config: dict[str, Any]
    ) -> None:
        self._inventory = inventory
        self._config = config
        self._entity_rooms = {
            entity_id: entity.room_id
            for entity_id, entity in inventory.entities.items()
        }
        self._sensor_rooms.clear()
        for room_id, room in inventory.rooms.items():
            for entity in room.temperature_sensors:
                self._sensor_rooms[entity.entity_id] = (room_id, "temperature")
            for entity in room.presence_sensors:
                self._sensor_rooms[entity.entity_id] = (room_id, "presence")

        managed: set[str] = set()
        excluded = set(config["excluded_rooms"])
        for group in inventory.gangs:
            if len(group.entity_ids) in {2, 3} and group.room_id not in excluded:
                managed.update(group.entity_ids)
        for room_id, room in inventory.rooms.items():
            if room_id in excluded:
                continue
            managed.update(entity.entity_id for entity in room.climates)
            managed.update(entity.entity_id for entity in room.lights)
            managed.update(entity.entity_id for entity in room.covers)
        managed.update(config["plug_offs"])
        managed.update(
            item["entity_id"] for item in config["heaters"] if item["turn_off"]
        )
        self._managed_entities = {
            entity_id
            for entity_id in managed
            if _domain(entity_id) in _MANAGED_DOMAINS
        }

    def _troubled_rooms(self, inventory: EnergyInventory) -> set[str]:
        troubled: set[str] = set()
        bad_keys: set[tuple[str | None, str, str | None]] = set()
        for room_id, room in inventory.rooms.items():
            bad = [
                entity
                for entity in room.sensor_entities
                if not entity.available
                or (
                    _domain(entity.entity_id) == "sensor"
                    and entity.temperature is None
                )
            ]
            if not bad:
                continue
            troubled.add(room_id)
            for entity in bad:
                bad_keys.add(
                    (room_id, "sensor_unavailable", entity.entity_id)
                )
                self._issue(
                    "sensor_unavailable",
                    room_id=room_id,
                    entity_id=entity.entity_id,
                    message="Room skipped because a sensor is unavailable.",
                )
            if self._engine.active_level == LEVEL_SMART and room.automatic:
                self._hass.async_create_task(
                    self._set_occupancy(
                        room_id, None, sensors_available=False
                    )
                )
        for key in list(self._issues):
            if key[1] == "sensor_unavailable" and key not in bad_keys:
                self._issues.pop(key, None)
        return troubled

    # -- static rules ------------------------------------------------------

    async def _apply_gangs(
        self,
        level: str,
        inventory: EnergyInventory,
        config: dict[str, Any],
        excluded: set[str],
        troubled: set[str],
    ) -> None:
        keepers_by_group = config["gang_keepers"]
        for group in inventory.gangs:
            count = len(group.entity_ids)
            if (
                group.room_id in excluded
                or group.room_id in troubled
                or count not in {2, 3}
                or (level == LEVEL_LOW and count == 2)
            ):
                continue
            keepers = set(keepers_by_group.get(group.group_id, []))
            expected = 2 if level == LEVEL_LOW else 1
            if len(keepers) != expected or not keepers.issubset(group.entity_ids):
                self._issue(
                    "invalid_gang_picks",
                    room_id=group.room_id,
                    entity_id=None,
                    message=f"Skipped {group.group_id}: keeper selection is stale.",
                )
                continue
            states = [inventory.entities[entity_id] for entity_id in group.entity_ids]
            if not all(entity.available and entity.on for entity in states):
                continue
            for entity in states:
                if entity.entity_id not in keepers:
                    await self._turn_off(entity.entity_id)

    async def _apply_common_offs(
        self,
        inventory: EnergyInventory,
        config: dict[str, Any],
        excluded: set[str],
        troubled: set[str],
    ) -> None:
        entity_ids = list(config["plug_offs"]) + [
            item["entity_id"]
            for item in config["heaters"]
            if item["turn_off"]
        ]
        for entity_id in dict.fromkeys(entity_ids):
            entity = inventory.entities.get(entity_id)
            if entity is None:
                self._issue(
                    "device_missing",
                    room_id=None,
                    entity_id=entity_id,
                    message="Configured device is missing; it was skipped.",
                )
                continue
            if entity.room_id in excluded or entity.room_id in troubled:
                continue
            if entity.available and entity.on:
                await self._turn_off(entity_id)

    async def _apply_static_climate(
        self,
        level: str,
        room: RoomInventory,
        config: dict[str, Any],
    ) -> None:
        if level == LEVEL_SMART:
            picks = config["ac_keepers"].get(room.room_id)
            if picks is None:
                return
            keepers = set(picks)
            candidates = {entity.entity_id for entity in room.climates}
            if not keepers.issubset(candidates):
                self._issue(
                    "invalid_ac_picks",
                    room_id=room.room_id,
                    entity_id=None,
                    message="Smart AC selection is stale; the room was skipped.",
                )
                return
            for entity in room.climates:
                if (
                    entity.entity_id not in keepers
                    and entity.available
                    and entity.state != "off"
                ):
                    await self._turn_off(entity.entity_id)
            return

        climates = list(room.climates)
        if level == LEVEL_MEDIUM and len(climates) > 1:
            picks = config["ac_keepers"].get(room.room_id)
            if not picks:
                self._issue(
                    "ac_keeper_required",
                    room_id=room.room_id,
                    entity_id=None,
                    message="Multiple ACs need one keeper; the room was skipped.",
                )
                return
            keepers = set(picks)
            if len(keepers) != 1 or not keepers.issubset(
                entity.entity_id for entity in climates
            ):
                self._issue(
                    "invalid_ac_picks",
                    room_id=room.room_id,
                    entity_id=None,
                    message="AC keeper selection is stale; the room was skipped.",
                )
                return
            for entity in climates:
                if (
                    entity.entity_id not in keepers
                    and entity.available
                    and entity.state != "off"
                ):
                    await self._turn_off(entity.entity_id)
            climates = [
                entity for entity in climates if entity.entity_id in keepers
            ]

        temperature = room.room_temperature
        for entity in climates:
            if not entity.available or entity.state == "off":
                continue
            mode = self._climate_mode(entity)
            if level == LEVEL_MEDIUM and self._temperature_kills(
                mode, temperature
            ):
                await self._turn_off(entity.entity_id)
                continue
            target = _as_float(entity.attributes.get("temperature"))
            if mode == "cool":
                if level == LEVEL_LOW and (target is None or target >= COOL_FLOOR):
                    continue
                await self._set_temperature(entity.entity_id, COOL_FLOOR)
                if level == LEVEL_MEDIUM:
                    await self._set_fan(entity, "low")
            elif mode == "heat":
                if level == LEVEL_LOW and (
                    target is None or target <= HEAT_CEILING
                ):
                    continue
                await self._set_temperature(entity.entity_id, HEAT_CEILING)
                if level == LEVEL_MEDIUM:
                    await self._set_fan(entity, "low")

    async def _apply_static_lights(
        self,
        level: str,
        room: RoomInventory,
        config: dict[str, Any],
    ) -> None:
        if len(room.lights) <= 1:
            return
        picks = config["light_keepers"].get(room.room_id)
        if picks is None:
            return
        keepers = set(picks)
        candidates = {entity.entity_id for entity in room.lights}
        expected = math.ceil(len(room.lights) / 2)
        if (
            len(keepers) != expected
            or not keepers.issubset(candidates)
        ):
            self._issue(
                "invalid_light_picks",
                room_id=room.room_id,
                entity_id=None,
                message="Light keeper selection is stale; the room was skipped.",
            )
            return
        cap = LIGHT_CAP_PCT[level]
        cap_raw = round(255 * cap / 100)
        for entity in room.lights:
            if not entity.available:
                continue
            if entity.entity_id not in keepers:
                if entity.on:
                    await self._turn_off(entity.entity_id)
            elif (
                entity.on
                and entity.brightness is not None
                and entity.brightness > cap_raw
            ):
                await self._turn_on_light(entity.entity_id, cap)

    async def _apply_static_covers(
        self, level: str, room: RoomInventory
    ) -> None:
        if level == LEVEL_LOW or not self._sun_context().heat_window:
            return
        for entity in room.covers:
            if entity.available and entity.state not in {"closed", "closing"}:
                await self._close_cover(entity.entity_id)

    # -- Smart occupancy ---------------------------------------------------

    async def _initialize_smart_room(self, room: RoomInventory) -> None:
        if not room.sensors_available or room.room_temperature is None:
            await self._set_occupancy(
                room.room_id, None, sensors_available=False
            )
            return
        if room.occupied:
            await self._set_occupancy(room.room_id, True)
            await self._apply_comfort_posture(room)
        else:
            self._schedule_empty(room.room_id)

    async def _async_presence_changed(self, room_id: str) -> None:
        if self._engine.active_level != LEVEL_SMART:
            return
        inventory = await self._refresh_inventory()
        room = inventory.rooms.get(room_id)
        if room is None or not room.automatic:
            return
        if not room.sensors_available or room.room_temperature is None:
            self._cancel_empty(room_id)
            self._cancel_boost(room_id)
            self._issue(
                "sensor_unavailable",
                room_id=room_id,
                entity_id=None,
                message="Room skipped because a sensor is unavailable.",
            )
            await self._set_occupancy(
                room_id, None, sensors_available=False
            )
            return
        if room.occupied:
            self._cancel_empty(room_id)
            await self._set_occupancy(room_id, True)
            await self._apply_comfort_posture(room)
        else:
            self._schedule_empty(room_id)

    @callback
    def _schedule_empty(self, room_id: str) -> None:
        if room_id in self._empty_timers:
            return

        @callback
        def _expired(_now: Any) -> None:
            self._empty_timers.pop(room_id, None)
            self._hass.async_create_task(self._finish_empty(room_id))

        self._empty_timers[room_id] = async_call_later(
            self._hass, EMPTY_GRACE_SECONDS, _expired
        )

    async def _finish_empty(self, room_id: str) -> None:
        if self._engine.active_level != LEVEL_SMART:
            return
        inventory = await self._refresh_inventory()
        room = inventory.rooms.get(room_id)
        if (
            room is None
            or not room.automatic
            or not room.sensors_available
            or room.room_temperature is None
            or room.occupied is not False
        ):
            return
        self._cancel_boost(room_id)
        await self._set_occupancy(room_id, False)
        cleared = await self._hass.async_add_executor_job(
            self._engine.clear_room_releases, room_id
        )
        if cleared:
            self._notify_changed()
        await self._apply_energy_posture(room)

    async def _apply_energy_posture(self, room: RoomInventory) -> None:
        config = self._config or {}
        for entity in room.climates:
            if entity.available and entity.state != "off":
                await self._turn_off(entity.entity_id, honor_release=False)
        for entity in room.lights:
            if entity.available and entity.on:
                await self._turn_off(entity.entity_id, honor_release=False)
        for entity_id in config.get("plug_offs", []):
            entity = (
                self._inventory.entities.get(entity_id)
                if self._inventory
                else None
            )
            if (
                entity is not None
                and entity.room_id == room.room_id
                and entity.available
                and entity.on
            ):
                await self._turn_off(entity_id, honor_release=False)
        if self._sun_context().heat_window:
            for entity in room.covers:
                if entity.available and entity.state not in {"closed", "closing"}:
                    await self._close_cover(
                        entity.entity_id, honor_release=False
                    )
        await self._record_event(
            EVENT_ENERGY_POSTURE,
            level=LEVEL_SMART,
            room_id=room.room_id,
        )

    async def _apply_comfort_posture(self, room: RoomInventory) -> None:
        temperature = room.room_temperature
        if temperature is None:
            return
        await self._apply_smart_climate(room, temperature)
        sun = self._sun_context()
        if not sun.day:
            for entity_id in self._automatic_light_keepers(room):
                entity = (
                    self._inventory.entities.get(entity_id)
                    if self._inventory
                    else None
                )
                if entity is not None and entity.available:
                    await self._turn_on_light(
                        entity_id, WELCOME_BRIGHTNESS_PCT
                    )
        for entity in room.covers:
            if not entity.available:
                continue
            if sun.day:
                if entity.state not in {"open", "opening"}:
                    await self._open_cover(entity.entity_id)
            elif entity.state not in {"closed", "closing"}:
                await self._close_cover(entity.entity_id)
        await self._record_event(
            EVENT_COMFORT_POSTURE,
            level=LEVEL_SMART,
            room_id=room.room_id,
        )

    def _automatic_light_keepers(self, room: RoomInventory) -> list[str]:
        configured = (self._config or {}).get("light_keepers", {}).get(
            room.room_id
        )
        candidates = {entity.entity_id for entity in room.lights}
        expected = math.ceil(len(room.lights) / 2)
        if (
            configured
            and len(configured) == expected
            and set(configured).issubset(candidates)
        ):
            return list(configured)
        ordered = sorted(
            room.lights,
            key=lambda entity: (
                entity.brightness is None,
                entity.entity_id,
            ),
        )
        return [entity.entity_id for entity in ordered[:expected]]

    async def _apply_smart_climate(
        self, room: RoomInventory, temperature: float
    ) -> None:
        self._cancel_boost(room.room_id)
        boosts: set[str] = set()
        boost_mode: str | None = None
        for entity in room.climates:
            if not entity.available:
                self._issue(
                    "device_unavailable",
                    room_id=room.room_id,
                    entity_id=entity.entity_id,
                    message="AC is unavailable; it was skipped.",
                )
                continue
            if self._engine.is_released(entity.entity_id):
                continue
            mode = self._climate_mode(entity)
            if mode == "heat":
                if temperature > HEAT_KILL_ABOVE:
                    await self._turn_off(entity.entity_id)
                elif temperature < HEAT_BOOST_BELOW:
                    await self._set_temperature(
                        entity.entity_id, HEAT_BOOST_TARGET, hvac_mode="heat"
                    )
                    await self._set_fan(entity, "max")
                    boosts.add(entity.entity_id)
                    boost_mode = "heat"
                else:
                    await self._settle_entity(entity, "heat")
            else:
                # Unknown/auto modes use the cooling-safe posture.  The
                # command includes hvac_mode=COOL only for an off climate;
                # active integrations retain their current cool mode.
                if temperature < COOL_KILL_BELOW:
                    await self._turn_off(entity.entity_id)
                elif temperature > COOL_BOOST_ABOVE:
                    await self._set_temperature(
                        entity.entity_id, COOL_BOOST_TARGET, hvac_mode="cool"
                    )
                    await self._set_fan(entity, "max")
                    boosts.add(entity.entity_id)
                    boost_mode = "cool"
                else:
                    await self._settle_entity(entity, "cool")
        if boosts and boost_mode is not None:
            self._start_boost(room.room_id, boost_mode, boosts)

    async def _settle_entity(
        self, entity: EnergyEntity, mode: str
    ) -> None:
        target = HEAT_CEILING if mode == "heat" else COOL_FLOOR
        await self._set_temperature(
            entity.entity_id, target, hvac_mode=mode
        )
        await self._set_fan(entity, "low")

    @callback
    def _start_boost(
        self, room_id: str, mode: str, entity_ids: set[str]
    ) -> None:
        @callback
        def _expired(_now: Any) -> None:
            self._boosts.pop(room_id, None)
            self._hass.async_create_task(
                self._settle_boost(room_id, mode, entity_ids, "failsafe")
            )

        cancel = async_call_later(
            self._hass, BOOST_FAILSAFE_SECONDS, _expired
        )
        self._boosts[room_id] = _Boost(
            room_id=room_id,
            mode=mode,
            entity_ids=set(entity_ids),
            cancel_timer=cancel,
        )
        self._hass.async_create_task(
            self._record_event(
                EVENT_BOOST_STARTED,
                level=LEVEL_SMART,
                room_id=room_id,
                data={"mode": mode, "entities": sorted(entity_ids)},
            )
        )

    async def _async_temperature_changed(self, room_id: str) -> None:
        level = self._engine.active_level
        if level not in {LEVEL_MEDIUM, LEVEL_SMART}:
            return
        inventory = await self._refresh_inventory()
        room = inventory.rooms.get(room_id)
        if room is None:
            return
        if not room.sensors_available or room.room_temperature is None:
            self._issue(
                "sensor_unavailable",
                room_id=room_id,
                entity_id=None,
                message="Room skipped because a sensor is unavailable.",
            )
            if level == LEVEL_SMART and room.automatic:
                self._cancel_boost(room_id)
                await self._set_occupancy(
                    room_id, None, sensors_available=False
                )
            return

        temperature = room.room_temperature
        if level == LEVEL_MEDIUM:
            for entity in room.climates:
                if (
                    not entity.available
                    or entity.state == "off"
                    or self._engine.is_released(entity.entity_id)
                ):
                    continue
                if self._temperature_kills(
                    self._climate_mode(entity), temperature
                ):
                    await self._turn_off(entity.entity_id)
            return

        occupancy = self._engine.snapshot()["room_occupancy"].get(
            room_id, {}
        )
        if room.automatic and not occupancy.get("sensors_available", False):
            if room.occupied:
                self._cancel_empty(room_id)
                await self._set_occupancy(room_id, True)
                await self._apply_comfort_posture(room)
            else:
                self._schedule_empty(room_id)
            return

        boost = self._boosts.get(room_id)
        if boost is None:
            return
        threshold_reached = (
            boost.mode == "cool" and temperature <= COOL_BOOST_ABOVE
        ) or (
            boost.mode == "heat" and temperature >= HEAT_BOOST_BELOW
        )
        if threshold_reached:
            boost.cancel_timer()
            self._boosts.pop(room_id, None)
            await self._settle_boost(
                room_id,
                boost.mode,
                boost.entity_ids,
                "temperature",
            )

    async def _settle_boost(
        self,
        room_id: str,
        mode: str,
        entity_ids: Iterable[str],
        reason: str,
    ) -> None:
        if self._engine.active_level != LEVEL_SMART:
            return
        inventory = await self._refresh_inventory()
        room = inventory.rooms.get(room_id)
        if (
            room is None
            or room.occupied is not True
            or not room.sensors_available
        ):
            return
        settled: list[str] = []
        for entity_id in entity_ids:
            if self._engine.is_released(entity_id):
                continue
            entity = inventory.entities.get(entity_id)
            if entity is None:
                continue
            await self._settle_entity(entity, mode)
            settled.append(entity_id)
        if settled:
            await self._record_event(
                EVENT_BOOST_SETTLED,
                level=LEVEL_SMART,
                room_id=room_id,
                data={
                    "mode": mode,
                    "reason": reason,
                    "entities": sorted(settled),
                },
            )

    # -- sun clock ---------------------------------------------------------

    def _sun_context(self) -> SunContext:
        state = (
            self._hass.states.get("sun.sun")
            if hasattr(self._hass.states, "get")
            else None
        )
        if state is None or state.state in _UNAVAILABLE_STATES:
            return SunContext(False, False, None)
        now = datetime.fromtimestamp(self._wall_clock(), timezone.utc)
        day = state.state == "above_horizon"
        attrs = dict(getattr(state, "attributes", {}) or {})
        next_rising = _as_datetime(attrs.get("next_rising"))
        next_setting = _as_datetime(attrs.get("next_setting"))
        last_changed = _as_datetime(getattr(state, "last_changed", None))

        start: datetime | None = None
        end: datetime | None = None
        if day and last_changed is not None:
            start = last_changed + timedelta(hours=1)
            if next_setting is not None:
                end = next_setting - timedelta(hours=1)
        heat_window = bool(
            day
            and start is not None
            and end is not None
            and start <= now < end
        )
        if day and start is not None and now < start:
            next_start = start
        elif next_rising is not None:
            next_start = next_rising + timedelta(hours=1)
        else:
            next_start = None
        return SunContext(day, heat_window, next_start)

    @callback
    def _schedule_sun_timer(self) -> None:
        if self._cancel_sun_timer is not None:
            self._cancel_sun_timer()
            self._cancel_sun_timer = None
        if self._engine.active_level not in {LEVEL_MEDIUM, LEVEL_SMART}:
            return
        start = self._sun_context().next_heat_window_start
        if start is None:
            return
        now = datetime.fromtimestamp(self._wall_clock(), timezone.utc)
        delay = max(0.0, (start - now).total_seconds())
        self._cancel_sun_timer = async_call_later(
            self._hass, delay, self._on_heat_window_start
        )

    @callback
    def _on_heat_window_start(self, _now: Any) -> None:
        self._cancel_sun_timer = None
        self._hass.async_create_task(self._async_heat_window_start())

    async def _async_heat_window_start(self) -> None:
        level = self._engine.active_level
        if level not in {LEVEL_MEDIUM, LEVEL_SMART}:
            return
        inventory = await self._refresh_inventory()
        excluded = set((self._config or {}).get("excluded_rooms", []))
        troubled = self._troubled_rooms(inventory)
        for room_id, room in inventory.rooms.items():
            if room_id in excluded or room_id in troubled:
                continue
            if level == LEVEL_SMART and room.automatic:
                occupancy = self._engine.snapshot()["room_occupancy"].get(
                    room_id, {}
                )
                if occupancy.get("occupied") is True:
                    continue
            for entity in room.covers:
                if entity.state not in {"closed", "closing"}:
                    await self._close_cover(entity.entity_id)
        self._schedule_sun_timer()

    async def _async_sun_changed(self) -> None:
        level = self._engine.active_level
        if level not in {LEVEL_MEDIUM, LEVEL_SMART}:
            return
        self._schedule_sun_timer()
        if level != LEVEL_SMART:
            return
        inventory = await self._refresh_inventory()
        day = self._sun_context().day
        occupancy = self._engine.snapshot()["room_occupancy"]
        for room_id, room in inventory.rooms.items():
            if not room.automatic:
                continue
            if occupancy.get(room_id, {}).get("occupied") is not True:
                continue
            for entity in room.covers:
                if not entity.available:
                    continue
                if day and entity.state not in {"open", "opening"}:
                    await self._open_cover(entity.entity_id)
                elif not day and entity.state not in {"closed", "closing"}:
                    await self._close_cover(entity.entity_id)

    # -- state listener / release ledger ----------------------------------

    @callback
    def _on_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if not _state_changed(old_state, new_state):
            return

        if entity_id == "sun.sun":
            self._hass.async_create_task(self._async_sun_changed())
            return
        sensor = self._sensor_rooms.get(entity_id)
        if sensor is not None:
            room_id, kind = sensor
            if kind == "presence":
                self._hass.async_create_task(
                    self._async_presence_changed(room_id)
                )
            else:
                self._hass.async_create_task(
                    self._async_temperature_changed(room_id)
                )
            return
        if (
            self._engine.active_level is None
            or entity_id not in self._managed_entities
            or self._consume_own_command(entity_id)
        ):
            return
        if (
            new_state is None
            or str(new_state.state) in _UNAVAILABLE_STATES
            or (
                old_state is not None
                and str(old_state.state) in _UNAVAILABLE_STATES
            )
        ):
            return
        self._hass.async_create_task(self._mark_released(entity_id))

    async def _mark_released(self, entity_id: str) -> None:
        room_id = self._entity_rooms.get(entity_id)
        changed = await self._hass.async_add_executor_job(
            lambda: self._engine.mark_released(
                entity_id, room_id=room_id, source="external"
            )
        )
        if changed:
            self._notify_changed()
            for room_id, boost in list(self._boosts.items()):
                boost.entity_ids.discard(entity_id)
                if not boost.entity_ids:
                    self._cancel_boost(room_id)

    def _note_own_command(self, entity_id: str) -> None:
        self._prune_own_commands()
        grace = (
            COVER_COMMAND_GRACE_SECONDS
            if _domain(entity_id) == "cover"
            else OWN_COMMAND_GRACE_SECONDS
        )
        self._own_commands[entity_id] = (
            self._monotonic_clock() + grace
        )

    def _consume_own_command(self, entity_id: str) -> bool:
        self._prune_own_commands()
        return entity_id in self._own_commands

    def _prune_own_commands(self) -> None:
        now = self._monotonic_clock()
        for entity_id, deadline in list(self._own_commands.items()):
            if deadline <= now:
                self._own_commands.pop(entity_id, None)

    # -- HA commands ------------------------------------------------------

    async def _command(
        self,
        entity_id: str,
        service: str,
        data: dict[str, Any] | None = None,
        *,
        honor_release: bool = True,
    ) -> bool:
        if honor_release and self._engine.is_released(entity_id):
            return False
        domain = _domain(entity_id)
        if domain not in _MANAGED_DOMAINS:
            self._issue(
                "unsupported_device",
                room_id=self._entity_rooms.get(entity_id),
                entity_id=entity_id,
                message="Configured device has an unsupported domain.",
            )
            return False
        self._note_own_command(entity_id)
        try:
            await self._hass.services.async_call(
                domain,
                service,
                {**(data or {}), "entity_id": entity_id},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - one device must not abort home
            self._failure_count += 1
            self._own_commands.pop(entity_id, None)
            self._issue(
                "command_failed",
                room_id=self._entity_rooms.get(entity_id),
                entity_id=entity_id,
                message=f"{service} failed: {type(err).__name__}",
            )
            _LOGGER.warning(
                "Energy Saving command %s.%s failed for %s: %s",
                domain,
                service,
                entity_id,
                err,
            )
            await self._record_event(
                EVENT_RULE_FAILED,
                level=self._engine.active_level,
                entity_id=entity_id,
                room_id=self._entity_rooms.get(entity_id),
                data={"domain": domain, "service": service},
            )
            return False
        self._command_count += 1
        return True

    async def _turn_off(
        self, entity_id: str, *, honor_release: bool = True
    ) -> bool:
        service = "turn_off"
        return await self._command(
            entity_id, service, honor_release=honor_release
        )

    async def _turn_on_light(
        self, entity_id: str, brightness_pct: int
    ) -> bool:
        return await self._command(
            entity_id,
            "turn_on",
            {"brightness_pct": brightness_pct},
        )

    async def _set_temperature(
        self,
        entity_id: str,
        temperature: float,
        *,
        hvac_mode: str | None = None,
    ) -> bool:
        data: dict[str, Any] = {"temperature": temperature}
        current = self._inventory.entities.get(entity_id) if self._inventory else None
        if hvac_mode is not None and (
            current is None or current.state == "off"
        ):
            data["hvac_mode"] = hvac_mode
        return await self._command(entity_id, "set_temperature", data)

    async def _set_fan(self, entity: EnergyEntity, desired: str) -> bool:
        modes = [
            str(mode)
            for mode in (entity.attributes.get("fan_modes") or [])
        ]
        selected = self._fan_mode(modes, desired)
        if selected is None:
            self._issue(
                "fan_mode_unsupported",
                room_id=entity.room_id,
                entity_id=entity.entity_id,
                message=f"AC does not expose a usable {desired} fan mode.",
            )
            return False
        return await self._command(
            entity.entity_id, "set_fan_mode", {"fan_mode": selected}
        )

    async def _open_cover(
        self, entity_id: str, *, honor_release: bool = True
    ) -> bool:
        return await self._command(
            entity_id, "open_cover", honor_release=honor_release
        )

    async def _close_cover(
        self, entity_id: str, *, honor_release: bool = True
    ) -> bool:
        return await self._command(
            entity_id, "close_cover", honor_release=honor_release
        )

    @staticmethod
    def _fan_mode(modes: list[str], desired: str) -> str | None:
        if not modes:
            return None
        lowered = {mode.lower(): mode for mode in modes}
        preferences = (
            ("low", "min", "minimum", "silent", "1")
            if desired == "low"
            else ("max", "maximum", "turbo", "high", "3")
        )
        for candidate in preferences:
            if candidate in lowered:
                return lowered[candidate]
        return None

    @staticmethod
    def _climate_mode(entity: EnergyEntity) -> str:
        mode = str(
            entity.attributes.get("hvac_mode") or entity.state
        ).lower()
        return "heat" if mode.startswith("heat") else "cool"

    @staticmethod
    def _temperature_kills(
        mode: str, room_temperature: float | None
    ) -> bool:
        if room_temperature is None:
            return False
        return (
            mode == "heat" and room_temperature > HEAT_KILL_ABOVE
        ) or (
            mode != "heat" and room_temperature < COOL_KILL_BELOW
        )

    # -- helpers -----------------------------------------------------------

    async def _refresh_inventory(self) -> EnergyInventory:
        inventory = await self._builder.async_build()
        if self._config is not None:
            self._set_context(inventory, self._config)
        self._troubled_rooms(inventory)
        return inventory

    async def _set_occupancy(
        self,
        room_id: str,
        occupied: bool | None,
        *,
        sensors_available: bool = True,
    ) -> bool:
        changed = await self._hass.async_add_executor_job(
            lambda: self._engine.set_room_occupancy(
                room_id,
                occupied,
                sensors_available=sensors_available,
            )
        )
        if changed:
            self._notify_changed()
        return changed

    async def _record_event(
        self,
        kind: str,
        *,
        level: str | None,
        entity_id: str | None = None,
        room_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._hass.async_add_executor_job(
                lambda: self._engine.record_event(
                    kind,
                    level=level,
                    entity_id=entity_id,
                    room_id=room_id,
                    data=data,
                )
            )
        except Exception:  # noqa: BLE001 - audit failure must not block rules
            _LOGGER.exception("Could not record Energy Saving event %s", kind)

    def _issue(
        self,
        code: str,
        *,
        room_id: str | None,
        entity_id: str | None,
        message: str,
    ) -> None:
        key = (room_id, code, entity_id)
        if key in self._issues:
            return
        self._issues[key] = {
            "code": code,
            "room_id": room_id,
            "entity_id": entity_id,
            "message": message,
        }
        if code == "sensor_unavailable":
            self._hass.async_create_task(
                self._record_event(
                    EVENT_ROOM_SKIPPED,
                    level=self._engine.active_level,
                    room_id=room_id,
                    entity_id=entity_id,
                    data={"reason": code},
                )
            )

    @callback
    def _cancel_empty(self, room_id: str) -> None:
        cancel = self._empty_timers.pop(room_id, None)
        if cancel is not None:
            cancel()

    @callback
    def _cancel_boost(self, room_id: str) -> None:
        boost = self._boosts.pop(room_id, None)
        if boost is not None:
            boost.cancel_timer()

    @callback
    def _cancel_all_timers(self) -> None:
        if self._cancel_sun_timer is not None:
            self._cancel_sun_timer()
            self._cancel_sun_timer = None
        for cancel in self._empty_timers.values():
            cancel()
        self._empty_timers.clear()
        for boost in self._boosts.values():
            boost.cancel_timer()
        self._boosts.clear()
