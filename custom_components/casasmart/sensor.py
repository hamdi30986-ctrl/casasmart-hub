"""CasaSmart sensors: the Energy Saving status and one sensor per paired device.

sensor.casasmart_energy_savings shows the active Energy Saving level (or
"off"), with the rest of the engine snapshot as attributes.

Each paired device gets a sensor.casasmart_user_<name> whose state is the
device's role (admin, sub-admin or user) and whose attributes hold its id,
name, rooms, pairing time and last-seen time. The sensors follow
EVENT_AUTH_CHANGED: they are added, updated and removed as devices pair,
change and unpair. last_seen is the auth engine's in-memory clock, so it is
None until the device's first authenticated call since the hub started.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import ENTITY_ID_FORMAT, SensorEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN, EVENT_AUTH_CHANGED, EVENT_ENERGY_CHANGED

if TYPE_CHECKING:
    from . import CasaSmartConfigEntry

_LOGGER = logging.getLogger(__name__)

# Polling only refreshes last_seen (an in-memory read); roster changes repaint
# at once through EVENT_AUTH_CHANGED.
SCAN_INTERVAL = timedelta(minutes=2)


def _iso(unix_seconds: float | int | None) -> str | None:
    """Unix seconds -> ISO-8601 UTC string; None for a missing or zero value."""
    if not unix_seconds:
        return None
    return datetime.fromtimestamp(float(unix_seconds), tz=UTC).isoformat()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the Energy Saving sensor and the per-device sensors."""
    async_add_entities([CasaSmartEnergySavingsSensor(entry)])
    manager = _UserSensorManager(hass, entry, async_add_entities)
    await manager.async_start()


class CasaSmartEnergySavingsSensor(SensorEntity):
    """The Energy Saving state as an HA sensor."""

    _attr_name = "Energy savings"
    _attr_icon = "mdi:leaf"
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: CasaSmartConfigEntry) -> None:
        self._entry = entry
        self.entity_id = "sensor.casasmart_energy_savings"
        self._attr_unique_id = f"{entry.entry_id}_energy_savings"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_ENERGY_CHANGED, self._on_changed)
        )

    @callback
    def _on_changed(self, _event: Event) -> None:
        """Repaint on any Energy Saving change."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """The active Energy Saving level, or "off"."""
        return self._entry.runtime_data.energy.active_level or "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Engine snapshot fields plus the adapter's current issues."""
        state = self._entry.runtime_data.energy.snapshot()
        adapter = self._entry.runtime_data.energy_adapter
        return {
            "active": state["active"],
            "lockout_enabled": state["lockout_enabled"],
            "released_devices": state["release_count"],
            "room_occupancy": state["room_occupancy"],
            "issues": adapter.issues() if adapter is not None else [],
            "revision": state["revision"],
        }


class _UserSensorManager:
    """Keeps one user sensor per paired device for a hub entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CasaSmartConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._add = async_add_entities
        self._entities: dict[str, CasaSmartUserSensor] = {}
        # Ids handed out so far, so same-named devices added together don't
        # collide before they reach the state machine.
        self._entity_ids: set[str] = set()
        # EVENT_AUTH_CHANGED comes in bursts; one reconcile at a time, so two
        # can't both create the same entity.
        self._lock = asyncio.Lock()

    def _next_entity_id(self, record: dict[str, Any]) -> str:
        """A unique sensor.casasmart_user_<name> id for a new device.

        Same-named devices get _2, _3 and so on.
        """
        name = record.get("name") or record["device_id"]
        object_id = f"casasmart_user_{slugify(name)}"
        existing = set(self._hass.states.async_entity_ids()) | self._entity_ids
        entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, object_id, current_ids=existing
        )
        self._entity_ids.add(entity_id)
        return entity_id

    async def async_start(self) -> None:
        """Seed the sensors, then follow every roster change until unload."""
        await self._reconcile()
        self._entry.async_on_unload(
            self._hass.bus.async_listen(EVENT_AUTH_CHANGED, self._on_auth_changed)
        )

    @callback
    def _on_auth_changed(self, _event: Event) -> None:
        """Reconcile in a task, off the bus listener."""
        self._entry.async_create_task(self._hass, self._reconcile())

    async def _reconcile(self) -> None:
        """Add, update and remove sensors to match the paired devices.

        A removed device's sensor also leaves the entity registry, so its
        entity id is free for a device paired later.
        """
        async with self._lock:
            auth = self._entry.runtime_data.auth
            devices = await self._hass.async_add_executor_job(auth.list_devices)
            current = {device["device_id"]: device for device in devices}

            new_entities: list[CasaSmartUserSensor] = []
            for device_id, record in current.items():
                entity = self._entities.get(device_id)
                if entity is None:
                    entity = CasaSmartUserSensor(
                        self._entry.entry_id,
                        record,
                        self._next_entity_id(record),
                        self._entry.runtime_data.auth,
                    )
                    self._entities[device_id] = entity
                    new_entities.append(entity)
                else:
                    entity.update_record(record)
            if new_entities:
                self._add(new_entities)

            gone = [
                device_id for device_id in self._entities if device_id not in current
            ]
            if gone:
                registry = er.async_get(self._hass)
                for device_id in gone:
                    entity = self._entities.pop(device_id)
                    entity_id = entity.entity_id
                    self._entity_ids.discard(entity_id)
                    if entity_id and registry.async_get(entity_id) is not None:
                        registry.async_remove(entity_id)
                    else:
                        await entity.async_remove()
                _LOGGER.debug("Removed %d revoked user sensor(s)", len(gone))


class CasaSmartUserSensor(SensorEntity):
    """One paired device: its role as state, its details as attributes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-key"
    # Polled only to refresh last_seen (see SCAN_INTERVAL).
    _attr_should_poll = True

    def __init__(
        self,
        entry_id: str,
        record: dict[str, Any],
        entity_id: str,
        engine: Any,
    ) -> None:
        self._device_id = record["device_id"]
        self._record = record
        self._engine = engine
        self._attr_unique_id = f"{entry_id}_user_{self._device_id}"
        name = record.get("name") or self._device_id
        self._attr_name = f"User {name}"
        # Pinned, or has_entity_name would prefix the hub's device name. Only
        # applies on first registration.
        self.entity_id = entity_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    @callback
    def update_record(self, record: dict[str, Any]) -> None:
        """Take a new device record and repaint (once added to HA)."""
        self._record = record
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        """The device's role."""
        return self._record.get("role")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Device id, name, rooms, pairing time and last-seen time."""
        return {
            "device_id": self._device_id,
            "name": self._record.get("name"),
            "rooms": self._record.get("rooms"),
            "enrolled_at": _iso(self._record.get("paired_at")),
            "last_seen": _iso(self._engine.last_seen(self._device_id)),
        }
