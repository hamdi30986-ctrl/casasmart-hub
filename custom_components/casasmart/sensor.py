"""CasaSmart enrolled-user sensors — one per paired device.

Each enrolled device surfaces as ``sensor.casasmart_user_<name>`` whose STATE
is the device's role (``admin`` / ``sub-admin`` / ``user``) and whose
attributes carry ``device_id``, ``enrolled_at`` and ``last_seen`` (plus the
room scope + display name). They are the household roster as first-class HA
entities — visible on a dashboard, usable in automations ("notify me when a
new sub-admin pairs").

The set is dynamic: the platform seeds from the auth engine at setup, then
reconciles on every ``EVENT_AUTH_CHANGED`` (pair / role-or-room edit / unpair
/ admin recovery / pairing-code regeneration) — adding sensors for new
devices, removing them for gone ones, repainting the rest. ``last_seen`` is
the engine's in-memory liveness clock (see ``AuthEngine.list_devices``): None
until the device makes its first authenticated call this boot.
"""

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

from .const import DOMAIN, EVENT_AUTH_CHANGED

if TYPE_CHECKING:
    from . import CasaSmartConfigEntry

_LOGGER = logging.getLogger(__name__)

# Role/name/enrollment changes repaint instantly off EVENT_AUTH_CHANGED; this
# slow poll only refreshes the live `last_seen` clock (an in-memory read), so a
# couple of minutes of granularity is plenty and costs nothing.
SCAN_INTERVAL = timedelta(minutes=2)


def _iso(unix_seconds: float | int | None) -> str | None:
    """Unix seconds -> ISO-8601 UTC string, or None for falsy/absent."""
    if not unix_seconds:
        return None
    return datetime.fromtimestamp(float(unix_seconds), tz=timezone.utc).isoformat()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Seed the per-user sensors and keep them in sync with enrollment."""
    manager = _UserSensorManager(hass, entry, async_add_entities)
    await manager.async_start()


class _UserSensorManager:
    """Owns the live mapping of device id -> sensor entity for one hub entry."""

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
        # Entity ids handed out so far — seeds the uniqueness check so a batch
        # of same-named devices doesn't collide before any are in the state
        # machine.
        self._entity_ids: set[str] = set()
        # EVENT_AUTH_CHANGED can fire in bursts (a regeneration revokes then
        # mints); serialize reconciles so two of them can't both create the
        # same new entity.
        self._lock = asyncio.Lock()

    def _next_entity_id(self, record: dict[str, Any]) -> str:
        """A unique ``sensor.casasmart_user_<name>`` id for a new device.

        Pinned (not device-name-prefixed) to match the plan; deduped across
        the live state machine AND the ids already handed out this batch, so
        same-named devices get ``…_2`` / ``…_3`` rather than colliding.
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
    """One enrolled device: role as state, enrollment detail as attributes."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:account-key"
    # Polled (SCAN_INTERVAL) ONLY to refresh the live `last_seen` clock; role /
    # name / enrollment changes still repaint instantly via update_record.
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
        # Pin the plan's exact ``sensor.casasmart_user_<name>`` id (a
        # device-named entity would become ``sensor.casasmart_hub_…``). Set on
        # first registration only; still groups under the hub device below.
        self.entity_id = entity_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    @callback
    def update_record(self, record: dict[str, Any]) -> None:
        """Adopt a fresh roster record and repaint (no-op before added)."""
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
            # Read live from the engine's in-memory clock (cheap), so the slow
            # poll keeps it current without waiting for a roster change.
            "last_seen": _iso(self._engine.last_seen(self._device_id)),
        }
