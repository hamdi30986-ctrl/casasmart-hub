"""CasaSmart runtime component."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .auth_api import authenticate_request, json_body
from .auth_tokens import ROLE_ADMIN
from .const import DOMAIN
from .energy import (
    EnergyAlreadyActiveError,
    EnergyConfigError,
    EnergyInactiveError,
    EnergySetupRequiredError,
    UnknownEnergyLevelError,
    validate_level_config,
)
from .energy_adapter import EnergyInventoryBuilder
from .energy_validation import validate_config_against_discovery

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData


def _runtime(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _entity_name(entity: Any) -> str:
    value = entity.attributes.get("friendly_name")
    return value if isinstance(value, str) and value else entity.entity_id


def _dimmable(entity: Any) -> bool:
    if entity.brightness is not None:
        return True
    modes = entity.attributes.get("supported_color_modes")
    return bool(
        isinstance(modes, (list, tuple, set))
        and any(str(getattr(mode, "value", mode)) != "onoff" for mode in modes)
    )


def _candidate(entity: Any, *, dimmable: bool | None = None) -> dict[str, Any]:
    value = {
        "entity_id": entity.entity_id,
        "name": _entity_name(entity),
        "state": entity.state,
        "available": entity.available,
    }
    if dimmable is not None:
        value["dimmable"] = dimmable
    return value


def _power_sibling(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """CasaSmart runtime component."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    siblings = er.async_entries_for_device(
        registry, entry.device_id, include_disabled_entities=True
    )
    for sibling in sorted(siblings, key=lambda item: item.entity_id):
        if not sibling.entity_id.startswith("sensor."):
            continue
        state = hass.states.get(sibling.entity_id)
        device_class = (
            state.attributes.get("device_class") if state is not None else None
        ) or sibling.original_device_class
        device_class = getattr(device_class, "value", device_class)
        if (
            str(device_class or "").lower() != "power"
            and "power" not in sibling.entity_id
        ):
            continue
        return {
            "entity_id": sibling.entity_id,
            "state": state.state if state is not None else "unavailable",
            "unit": state.attributes.get("unit_of_measurement")
            if state is not None
            else None,
        }
    return None


async def async_energy_discovery(
    hass: HomeAssistant, runtime: CasaSmartRuntimeData
) -> dict[str, Any]:
    """CasaSmart runtime component."""
    builder = EnergyInventoryBuilder(hass, runtime.registry)
    inventory = await builder.async_build()

    floors, room_records, user_devices = await hass.async_add_executor_job(
        lambda: (
            runtime.registry.list_floors(),
            runtime.registry.list_rooms(),
            runtime.registry.list_user_devices(),
        )
    )
    rooms_by_id = {room["room_id"]: dict(room) for room in room_records}
    for room_id in inventory.rooms:
        rooms_by_id.setdefault(
            room_id,
            {
                "room_id": room_id,
                "name": runtime.registry.room_name(room_id) or room_id,
                "floor_id": None,
                "icon": None,
                "sort_order": 0,
            },
        )

    gang_rows: dict[str, list[dict[str, Any]]] = {}
    typed_gangs: dict[str, dict[str, Any]] = {}
    for device in user_devices:
        controls = [
            item
            for item in device.get("control_entity_ids", [])
            if isinstance(item, str)
        ]
        nested = device.get("gangs")
        nested = nested if isinstance(nested, dict) else {}
        legacy_types = device.get("gang_types")
        legacy_types = legacy_types if isinstance(legacy_types, dict) else {}
        legacy_names = device.get("gang_names")
        legacy_names = legacy_names if isinstance(legacy_names, dict) else {}
        legacy_keys = list(legacy_types)
        if not controls or not (nested or legacy_types):
            continue
        channels: list[dict[str, Any]] = []
        room_ids: set[str] = set()
        for index, entity_id in enumerate(controls):
            metadata = nested.get(entity_id)
            if not isinstance(metadata, dict):
                metadata = {}
            if not metadata and legacy_types:
                object_id = entity_id.partition(".")[2]
                matching_key = next(
                    (
                        key
                        for key in legacy_keys
                        if object_id == key or object_id.endswith(f"_{key}")
                    ),
                    legacy_keys[index] if index < len(legacy_keys) else None,
                )
                if matching_key is not None:
                    metadata = {
                        "type": legacy_types.get(matching_key),
                        "name": legacy_names.get(matching_key),
                    }
            room_id = metadata.get("room_id") or device.get("room_id")
            entity = inventory.entities.get(entity_id)
            if not isinstance(room_id, str) and entity is not None:
                room_id = entity.room_id
            if isinstance(room_id, str):
                room_ids.add(room_id)
            gang_type = metadata.get("type")
            typed_gangs[entity_id] = {
                "type": gang_type,
                "room_id": room_id if isinstance(room_id, str) else None,
            }
            channels.append(
                {
                    "entity_id": entity_id,
                    "channel_index": index,
                    "name": metadata.get("name")
                    or (_entity_name(entity) if entity is not None else entity_id),
                    "type": gang_type,
                    "presentation": metadata.get("presentation", "grouped"),
                    "available": entity.available if entity is not None else False,
                }
            )
        room_id = (
            device.get("room_id")
            if isinstance(device.get("room_id"), str)
            else next(iter(sorted(room_ids)), None)
        )
        if room_id is None:
            continue
        gang_rows.setdefault(room_id, []).append(
            {
                "group_id": str(device.get("ha_device_id") or controls[0]),
                "name": device.get("custom_name")
                or str(device.get("ha_device_id") or controls[0]),
                "channels": channels,
                "channel_count": len(channels),
            }
        )

    output_rooms: list[dict[str, Any]] = []
    for room_id, record in rooms_by_id.items():
        room = inventory.rooms.get(room_id)
        climates = list(room.climates) if room is not None else []
        lights = list(room.lights) if room is not None else []
        covers = list(room.covers) if room is not None else []
        temperatures = list(room.temperature_sensors) if room is not None else []
        presences = list(room.presence_sensors) if room is not None else []

        plugs: list[dict[str, Any]] = []
        heaters: list[dict[str, Any]] = []
        for entity in sorted(
            inventory.entities.values(), key=lambda item: item.entity_id
        ):
            if entity.room_id != room_id or not entity.entity_id.startswith("switch."):
                continue
            typed = typed_gangs.get(entity.entity_id, {})
            gang_type = typed.get("type")
            device_class = str(entity.attributes.get("device_class", "")).lower()
            if gang_type in {"outlet", "heater"} or device_class in {"outlet", "plug"}:
                item = _candidate(entity)
                item["power_sensor"] = _power_sibling(hass, entity.entity_id)
                plugs.append(item)
            if gang_type in {"heater", "outlet"}:
                heaters.append(_candidate(entity))

        output_rooms.append(
            {
                **record,
                "automatic": bool(temperatures and presences),
                "climates": [_candidate(item) for item in climates],
                "lights": [
                    _candidate(item, dimmable=_dimmable(item)) for item in lights
                ],
                "gangs": sorted(
                    gang_rows.get(room_id, []), key=lambda item: item["group_id"]
                ),
                "plugs": plugs,
                "covers": [_candidate(item) for item in covers],
                "heaters": heaters,
                "sensors": {
                    "temperature": [_candidate(item) for item in temperatures],
                    "presence": [_candidate(item) for item in presences],
                },
            }
        )

    output_rooms.sort(
        key=lambda item: (
            item.get("floor_id") or "",
            item.get("sort_order", 0),
            item.get("name", ""),
            item["room_id"],
        )
    )
    floor_rows = []
    for floor in sorted(
        floors,
        key=lambda item: (
            item.get("sort_order", 0),
            item.get("name", ""),
            item["floor_id"],
        ),
    ):
        floor_rows.append(
            {
                **floor,
                "room_ids": [
                    room["room_id"]
                    for room in output_rooms
                    if room.get("floor_id") == floor["floor_id"]
                ],
            }
        )
    unassigned = [
        room["room_id"] for room in output_rooms if room.get("floor_id") is None
    ]
    return {
        "floors": floor_rows,
        "unassigned_room_ids": unassigned,
        "rooms": output_rooms,
    }


class _EnergyView(HomeAssistantView):
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _ready(self) -> tuple[CasaSmartRuntimeData | None, web.Response | None]:
        runtime = _runtime(self._hass)
        if runtime is None or runtime.energy_controller is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return runtime, None

    def _energy_error(self, err: Exception) -> web.Response:
        if isinstance(err, EnergySetupRequiredError):
            return self.json(
                {"error": "setup_required", "level": err.level},
                HTTPStatus.CONFLICT,
            )
        if isinstance(err, EnergyAlreadyActiveError):
            return self.json({"error": "already_active"}, HTTPStatus.CONFLICT)
        if isinstance(err, EnergyInactiveError):
            return self.json({"error": "energy_inactive"}, HTTPStatus.CONFLICT)
        if isinstance(err, UnknownEnergyLevelError):
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        return self.json_message(str(err), HTTPStatus.BAD_REQUEST)


class CasaSmartEnergyStateView(_EnergyView):
    url = f"/api/{DOMAIN}/energy/state"
    name = f"api:{DOMAIN}:energy:state"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "energy.read")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        state = await runtime.energy_controller.async_state()
        if claims.get("role") != ROLE_ADMIN:


            for field in (
                "released_entities",
                "release_details",
                "release_count",
                "issues",
                "stats",
            ):
                state.pop(field, None)
        scope = claims.get("rooms")
        if scope is not None:
            allowed_rooms = set(scope)
            state["room_occupancy"] = {
                room_id: value
                for room_id, value in state["room_occupancy"].items()
                if room_id in allowed_rooms
            }
        return self.json(state)


class CasaSmartEnergyDiscoveryView(_EnergyView):
    url = f"/api/{DOMAIN}/energy/discovery"
    name = f"api:{DOMAIN}:energy:discovery"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "energy.manage")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        return self.json(await async_energy_discovery(self._hass, runtime))


class CasaSmartEnergyConfigView(_EnergyView):
    url = f"/api/{DOMAIN}/energy/config/{{level}}"
    name = f"api:{DOMAIN}:energy:config"

    async def get(self, request: web.Request, level: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "energy.manage")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        try:
            config = await self._hass.async_add_executor_job(
                runtime.energy.get_config, level
            )
        except UnknownEnergyLevelError as err:
            return self._energy_error(err)
        return self.json(config)

    async def patch(self, request: web.Request, level: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "energy.manage")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        if "lockout_enabled" in payload and not isinstance(
            payload["lockout_enabled"], bool
        ):
            return self.json_message(
                "lockout_enabled must be a boolean", HTTPStatus.BAD_REQUEST
            )
        try:
            preview = await self._hass.async_add_executor_job(
                runtime.energy.get_config, level
            )
            preview.update(payload)
            normalized = validate_level_config(level, preview)
            discovery = await async_energy_discovery(self._hass, runtime)
            validate_config_against_discovery(level, normalized, discovery)
            config = await self._hass.async_add_executor_job(
                lambda: runtime.energy.patch_config(
                    level, payload, actor=claims.get("sub")
                )
            )
        except (EnergyConfigError, UnknownEnergyLevelError) as err:
            return self._energy_error(err)
        runtime.energy_controller.notify_changed()
        return self.json(config)

    async def delete(self, request: web.Request, level: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "energy.manage")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        try:
            config = await self._hass.async_add_executor_job(
                lambda: runtime.energy.reset_config(level, actor=claims.get("sub"))
            )
        except UnknownEnergyLevelError as err:
            return self._energy_error(err)
        runtime.energy_controller.notify_changed()
        return self.json(config)


class CasaSmartEnergyActivateView(_EnergyView):
    url = f"/api/{DOMAIN}/energy/activate"
    name = f"api:{DOMAIN}:energy:activate"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "energy.control")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        if "lockout_enabled" in payload and not isinstance(
            payload["lockout_enabled"], bool
        ):
            return self.json_message(
                "lockout_enabled must be a boolean", HTTPStatus.BAD_REQUEST
            )
        try:
            result = await runtime.energy_controller.async_activate(
                payload.get("level"),
                smart_lockout_enabled=payload.get("lockout_enabled"),
                actor=claims.get("sub"),
            )
        except (
            EnergyConfigError,
            EnergySetupRequiredError,
            EnergyAlreadyActiveError,
            UnknownEnergyLevelError,
        ) as err:
            return self._energy_error(err)
        return self.json(result)


class CasaSmartEnergyDeactivateView(_EnergyView):
    url = f"/api/{DOMAIN}/energy/deactivate"
    name = f"api:{DOMAIN}:energy:deactivate"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "energy.control")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        return self.json(
            await runtime.energy_controller.async_deactivate(actor=claims.get("sub"))
        )


class CasaSmartEnergyReapplyView(_EnergyView):
    url = f"/api/{DOMAIN}/energy/reapply"
    name = f"api:{DOMAIN}:energy:reapply"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "energy.control")
        if error is not None:
            return error
        runtime, error = self._ready()
        if error is not None:
            return error
        try:
            result = await runtime.energy_controller.async_reapply(
                actor=claims.get("sub")
            )
        except EnergyInactiveError as err:
            return self._energy_error(err)
        return self.json(result)
