"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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




SCAN_INTERVAL = timedelta(minutes=2)


def _iso(unix_seconds: float | int | None) -> str | None:
    """CasaSmart runtime component."""
    if not unix_seconds:
        return None
    return datetime.fromtimestamp(float(unix_seconds), tz=timezone.utc).isoformat()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """CasaSmart runtime component."""
    async_add_entities([CasaSmartEnergySavingsSensor(entry)])
    manager = _UserSensorManager(hass, entry, async_add_entities)
    await manager.async_start()


class CasaSmartEnergySavingsSensor(SensorEntity):
    """CasaSmart runtime component."""

    _attr_name = "CasaSmart Energy Savings"
    _attr_icon = "mdi:leaf"
    _attr_should_poll = False
    _attr_has_entity_name = False

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
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self._entry.runtime_data.energy.active_level or "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
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
    """CasaSmart runtime component."""

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



        self._entity_ids: set[str] = set()



        self._lock = asyncio.Lock()

    def _next_entity_id(self, record: dict[str, Any]) -> str:
        """CasaSmart runtime component."""
        name = record.get("name") or record["device_id"]
        object_id = f"casasmart_user_{slugify(name)}"
        existing = set(self._hass.states.async_entity_ids()) | self._entity_ids
        entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, object_id, current_ids=existing
        )
        self._entity_ids.add(entity_id)
        return entity_id

    async def async_start(self) -> None:
        await self._reconcile()
        self._entry.async_on_unload(
            self._hass.bus.async_listen(EVENT_AUTH_CHANGED, self._on_auth_changed)
        )

    @callback
    def _on_auth_changed(self, _event: Event) -> None:
        self._entry.async_create_task(self._hass, self._reconcile())

    async def _reconcile(self) -> None:
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
    """CasaSmart runtime component."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:account-key"


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
        self._attr_name = f"CasaSmart User {name}"



        self.entity_id = entity_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    @callback
    def update_record(self, record: dict[str, Any]) -> None:
        """CasaSmart runtime component."""
        self._record = record
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._record.get("role")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "name": self._record.get("name"),
            "rooms": self._record.get("rooms"),
            "enrolled_at": _iso(self._record.get("paired_at")),


            "last_seen": _iso(self._engine.last_seen(self._device_id)),
        }
