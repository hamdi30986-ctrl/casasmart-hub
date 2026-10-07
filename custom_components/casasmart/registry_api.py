"""Registry REST endpoints over RegistryEngine.

Floors, rooms, room tags, entity assignments, room moves, user devices and
their gangs, scenes and per-member favorites. Reads are room-scoped, and
writes fire EVENT_REGISTRY_CHANGED so connected apps re-fetch.

async_execute_registry_scene runs a scene for the activate endpoint, the
casasmart.activate_scene service and suggestion actions.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .auth_api import authenticate_request, get_engine, json_body
from .auth_engine import AuthEngine
from .const import DOMAIN, EVENT_REGISTRY_CHANGED
from .energy_runtime import energy_lockout_applies
from .entity_bridge import CommandError, validate_command
from .filtering import area_id_of, ha_area_id_of, in_scope, is_assignable, is_served
from .registry import (
    IDEMPOTENCY_KEY,
    MAX_DEVICE_ENTITIES,
    UNSET,
    InUseError,
    RegistryEngine,
    RegistryError,
    RoomMoveDenied,
    UnknownItemError,
)
from .runtime_lookup import loaded_runtime_data
from .storage import StorageError

_LOGGER = logging.getLogger(__name__)

# Per-step ceiling on a scene's service call: one stuck device must not hold
# up the rest of the scene.
_SCENE_CALL_TIMEOUT = 10.0


def get_registry(hass: HomeAssistant) -> RegistryEngine | None:
    """The loaded entry's registry engine, or None when not set up."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.registry if runtime_data is not None else None


def _scene_entity_ids(entities: Any) -> list[str]:
    """The string entity_ids in a scene's entities payload."""
    if not isinstance(entities, list):
        return []
    return [
        e["entity_id"]
        for e in entities
        if isinstance(e, dict) and isinstance(e.get("entity_id"), str)
    ]


async def async_execute_registry_scene(
    hass: HomeAssistant, scene: dict[str, Any]
) -> dict[str, Any]:
    """Run a scene's commands in order and report a result per entity.

    Each step is checked again as it runs: the entity must exist and be served,
    and its command passes the same whitelist as a single-device command. A
    failing or timed-out step is reported and the rest still run. Callers own
    the scope and Energy Saving checks.
    """
    scene_id = scene["scene_id"]
    generated = scene.get("generated_room_v1", False)

    # An IR AC takes every climate call as a full-state burst, and a second one
    # right after the mode is set collides with it, so saved scenes (not
    # generated ones) skip the fan-mode step for an AC whose mode they set.
    climate_with_mode = {
        item["entity_id"]
        for item in scene["entities"]
        if item["entity_id"].split(".", 1)[0] == "climate"
        and item.get("action") in ("set_temperature", "set_hvac_mode")
    }
    entities_to_run = [
        item
        for item in scene["entities"]
        if generated
        or not (
            item["entity_id"].split(".", 1)[0] == "climate"
            and item.get("action") == "set_fan_mode"
            and item["entity_id"] in climate_with_mode
        )
    ]

    results = []
    for item in entities_to_run:
        entity_id = item["entity_id"]

        if generated:
            # Each call yields to the event loop, so check again that the step
            # still applies: the device may have moved or been switched off.
            from .generated_suggestions import room_actions

            state = hass.states.get(entity_id)
            valid = (
                state is not None and area_id_of(hass, entity_id) == scene["room_id"]
            )
            if valid and entity_id.startswith("light."):
                valid = (
                    state.state == "on"
                    and state.attributes.get("casasmart_room_activity_exclude")
                    is not True
                )
                if valid and item["action"] == "turn_on":
                    valid = any(
                        a["action"] == item["action"]
                        and a["data"] == item.get("data", {})
                        for a in room_actions([state], "room_eco")
                    )
            elif valid:
                unit = getattr(
                    getattr(hass.config, "units", None), "temperature_unit", "°C"
                )
                valid = any(
                    a["action"] == item["action"] and a["data"] == item.get("data", {})
                    for a in room_actions([state], scene["kind"], temperature_unit=unit)
                )
            if not valid:
                results.append(
                    {
                        "entity_id": entity_id,
                        "ok": False,
                        "error": "Device changed since preview",
                    }
                )
                continue

        if hass.states.get(entity_id) is None or not is_served(hass, entity_id):
            results.append(
                {
                    "entity_id": entity_id,
                    "ok": False,
                    "error": "Device not available",
                }
            )
            continue
        try:
            domain, service, service_data = validate_command(
                entity_id, item["action"], item.get("data")
            )
            await asyncio.wait_for(
                hass.services.async_call(
                    domain,
                    service,
                    {**service_data, "entity_id": entity_id},
                    blocking=True,
                ),
                timeout=_SCENE_CALL_TIMEOUT,
            )
            results.append({"entity_id": entity_id, "ok": True})
        except TimeoutError:
            _LOGGER.warning(
                "Scene %s: %s on %s timed out", scene_id, item["action"], entity_id
            )
            results.append({"entity_id": entity_id, "ok": False, "error": "Timed out"})
        except (CommandError, HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning(
                "Scene %s: %s on %s failed: %s",
                scene_id,
                item["action"],
                entity_id,
                err,
            )
            results.append({"entity_id": entity_id, "ok": False, "error": str(err)})

    return {
        "scene_id": scene_id,
        "ok": all(result["ok"] for result in results),
        "results": results,
    }


class _RegistryView(HomeAssistantView):
    """Shared plumbing for the registry views below."""

    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _registry_or_503(
        self,
    ) -> tuple[RegistryEngine | None, web.Response | None]:
        """The registry engine, or a 503 response while the hub isn't set up."""
        registry = get_registry(self._hass)
        if registry is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return registry, None

    def _notify_change(self, kind: str) -> None:
        """Tell connected apps the organization changed (they re-fetch)."""
        self._hass.bus.async_fire(EVENT_REGISTRY_CHANGED, {"kind": kind})

    async def _call(
        self, func: Callable[..., Any], *args: Any
    ) -> tuple[Any, web.Response | None]:
        """Run a registry call in the executor.

        Returns (result, None), or (None, response) for a registry or storage
        error.
        """
        try:
            return await self._hass.async_add_executor_job(func, *args), None
        except RegistryError as err:
            return None, self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return None, self._storage_failure(err)

    def _error_response(self, err: RegistryError) -> web.Response:
        """Map a registry error to 404 (unknown item), 409 (in use) or 400."""
        if isinstance(err, UnknownItemError):
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        if isinstance(err, InUseError):
            return self.json_message(str(err), HTTPStatus.CONFLICT)
        return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

    def _storage_failure(self, err: Exception) -> web.Response:
        """Log a storage error and return a plain 500."""
        _LOGGER.error("Registry storage failure: %s", err)
        return self.json_message("Storage failure", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _energy_flag_reject(
        self, claims: dict[str, Any], payload: dict[str, Any]
    ) -> web.Response | None:
        """403 when the body sets the Energy Saving flag without energy.manage."""
        if "works_during_energy_saving" in payload and not AuthEngine.authorize(
            claims, "energy.manage"
        ):
            return self.json_message(
                "Energy Saving flags require admin access",
                HTTPStatus.FORBIDDEN,
            )
        return None

    def _unserved_scene_entity(self, entities: Any) -> web.Response | None:
        """404 when a scene step names an entity that isn't served.

        A scene can't store a command the app couldn't send directly. Shape
        errors are left to the engine.
        """
        if not isinstance(entities, list):
            return None
        for item in entities:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            if self._hass.states.get(entity_id) is None or not is_served(
                self._hass, entity_id
            ):
                return self.json_message(
                    f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
                )
        return None

    def _scope_reject(self, claims, *entity_lists):
        """400 when a write touches an entity outside the caller's rooms.

        Unknown and out-of-scope entities get the same message. Entities are not
        required to be served, so a device whose relays are dead or hidden can
        still be edited or deleted.
        """
        scope = claims.get("rooms")
        for entity_ids in entity_lists:
            if not isinstance(entity_ids, list):
                continue
            for entity_id in entity_ids:
                if not isinstance(entity_id, str) or not in_scope(
                    self._hass, entity_id, scope
                ):
                    return self.json_message(
                        f"Unknown device {entity_id!r}", HTTPStatus.BAD_REQUEST
                    )
        return None


class CasaSmartRegistryView(_RegistryView):
    """GET /api/casasmart/registry: the home's layout as the caller may see it.

    Floors, rooms, room tags, scenes and user devices, plus one entry per
    served entity with its resolved room and display name. A room-scoped
    caller gets only its rooms, the floors and tags they use, and the scenes
    and user devices whose entities are all in scope.
    """

    url = f"/api/{DOMAIN}/registry"
    name = f"api:{DOMAIN}:registry"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        scope = claims.get("rooms")

        def _read() -> tuple[list, list, list, dict, list, list]:
            return (
                registry.list_floors(),
                registry.list_rooms(),
                registry.list_room_tags(),
                registry.list_assignments(),
                registry.list_scenes(),
                registry.list_user_devices(),
            )

        try:
            (
                floors,
                rooms,
                room_tags,
                assignments,
                scenes,
                user_devices,
            ) = await self._hass.async_add_executor_job(_read)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)

        known_rooms = {room["room_id"] for room in rooms}

        if scope is not None:
            rooms = [room for room in rooms if room["room_id"] in scope]
        visible_room_ids = {room["room_id"] for room in rooms}
        room_tags = [
            {
                **tag,
                "room_ids": [
                    room_id
                    for room_id in tag.get("room_ids", [])
                    if room_id in visible_room_ids
                ],
            }
            for tag in room_tags
            if any(room_id in visible_room_ids for room_id in tag.get("room_ids", []))
        ]
        visible_floors = {room["floor_id"] for room in rooms} - {None}
        if scope is not None:
            floors = [floor for floor in floors if floor["floor_id"] in visible_floors]

        devices = []
        for state in self._hass.states.async_all():
            entity_id = state.entity_id
            if not is_served(self._hass, entity_id):
                continue
            if not in_scope(self._hass, entity_id, scope):
                continue
            record = assignments.get(entity_id, {})
            room_id = area_id_of(self._hass, entity_id)
            devices.append(
                {
                    "entity_id": entity_id,
                    "room_id": room_id if room_id in known_rooms else None,
                    "display_name": record.get("display_name"),
                    "sort_order": record.get("sort_order", 0),
                }
            )
        devices.sort(key=lambda device: device["entity_id"])

        if scope is not None:
            scenes = [
                scene
                for scene in scenes
                if all(
                    in_scope(self._hass, item["entity_id"], scope)
                    for item in scene["entities"]
                )
            ]

        for collection in (floors, rooms, room_tags, scenes):
            collection.sort(
                key=lambda item: (item.get("sort_order", 0), item.get("name", ""))
            )

        # A user device shows while one of its control entities still exists.
        user_devices = [
            device
            for device in user_devices
            if any(
                is_assignable(self._hass, entity_id)
                for entity_id in device.get("control_entity_ids", [])
            )
        ]
        if scope is not None:
            user_devices = [
                device
                for device in user_devices
                if device.get("control_entity_ids")
                and all(
                    in_scope(self._hass, entity_id, scope)
                    for entity_id in device.get("control_entity_ids", [])
                )
            ]

        return self.json(
            {
                "floors": floors,
                "rooms": rooms,
                "room_tags": room_tags,
                "devices": devices,
                "scenes": scenes,
                "user_devices": user_devices,
                "features": ["atomic_room_move_v1"],
            }
        )


class CasaSmartFloorsView(_RegistryView):
    """POST /api/casasmart/registry/floors: create a floor."""

    url = f"/api/{DOMAIN}/registry/floors"
    name = f"api:{DOMAIN}:registry:floors"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        floor, failed = await self._call(
            lambda: registry.create_floor(
                payload.get("name"), payload.get("sort_order")
            )
        )
        if failed is not None:
            return failed
        self._notify_change("floors")
        return self.json(floor, HTTPStatus.CREATED)


class CasaSmartFloorView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/floors/{floor_id}."""

    url = f"/api/{DOMAIN}/registry/floors/{{floor_id}}"
    name = f"api:{DOMAIN}:registry:floor"

    async def patch(self, request: web.Request, floor_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        floor, failed = await self._call(
            lambda: registry.update_floor(
                floor_id,
                payload.get("name", ...),
                payload.get("sort_order", ...),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("floors")
        return self.json(floor)

    async def delete(self, request: web.Request, floor_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        _, failed = await self._call(registry.delete_floor, floor_id)
        if failed is not None:
            return failed
        self._notify_change("floors")
        return self.json({"deleted": floor_id})


class CasaSmartRoomsView(_RegistryView):
    """POST /api/casasmart/registry/rooms: create a room."""

    url = f"/api/{DOMAIN}/registry/rooms"
    name = f"api:{DOMAIN}:registry:rooms"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        room, failed = await self._call(
            lambda: registry.create_room(
                payload.get("name"),
                payload.get("floor_id"),
                payload.get("icon"),
                payload.get("sort_order"),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("rooms")
        return self.json(room, HTTPStatus.CREATED)


class CasaSmartRoomView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/rooms/{room_id}."""

    url = f"/api/{DOMAIN}/registry/rooms/{{room_id}}"
    name = f"api:{DOMAIN}:registry:room"

    async def patch(self, request: web.Request, room_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        room, failed = await self._call(
            lambda: registry.update_room(
                room_id,
                name=payload.get("name", ...),
                floor_id=payload.get("floor_id", ...),
                icon=payload.get("icon", ...),
                sort_order=payload.get("sort_order", ...),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("rooms")
        return self.json(room)

    async def delete(self, request: web.Request, room_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        # An imported room's id is also an HA area that outlives it, so pin the
        # area's unrecorded entities to Unassigned or they'd stay in the room.
        ha_orphans = [
            state.entity_id
            for state in self._hass.states.async_all()
            if is_served(self._hass, state.entity_id)
            and registry.room_of(state.entity_id) is UNSET
            and ha_area_id_of(self._hass, state.entity_id) == room_id
        ]

        def _delete() -> int:
            unassigned = registry.delete_room(room_id)
            for entity_id in ha_orphans:
                registry.assign_device(entity_id, room_id=None)
            return unassigned + len(ha_orphans)

        unassigned, failed = await self._call(_delete)
        if failed is not None:
            return failed
        self._notify_change("rooms")
        return self.json({"deleted": room_id, "devices_unassigned": unassigned})


class CasaSmartRoomTagsView(_RegistryView):
    """POST /api/casasmart/registry/tags: create a room tag."""

    url = f"/api/{DOMAIN}/registry/tags"
    name = f"api:{DOMAIN}:registry:tags"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        tag, failed = await self._call(
            registry.create_room_tag,
            payload.get("name"),
            payload.get("color"),
            payload.get("room_ids"),
        )
        if failed is not None:
            return failed
        self._notify_change("room-tags")
        return self.json(tag, HTTPStatus.CREATED)


class CasaSmartRoomTagView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/tags/{tag_id}."""

    url = f"/api/{DOMAIN}/registry/tags/{{tag_id}}"
    name = f"api:{DOMAIN}:registry:tag"

    async def patch(self, request: web.Request, tag_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        tag, failed = await self._call(
            lambda: registry.update_room_tag(
                tag_id,
                name=payload.get("name", ...),
                color=payload.get("color", ...),
                room_ids=payload.get("room_ids", ...),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("room-tags")
        return self.json(tag)

    async def delete(self, request: web.Request, tag_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        _, failed = await self._call(registry.delete_room_tag, tag_id)
        if failed is not None:
            return failed
        self._notify_change("room-tags")
        return self.json({"deleted": tag_id})


class CasaSmartRoomMoveView(_RegistryView):
    """POST /api/casasmart/registry/room-moves: move a device to a room.

    Atomic and compare-and-set: the move applies only if the device's entities
    and their rooms still match what the client reviewed, and a retry with the
    same idempotency key replays the first result. Never controls hardware.
    """

    url = f"/api/{DOMAIN}/registry/room-moves"
    name = f"api:{DOMAIN}:registry:room-move"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if not isinstance(payload, dict):
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        key = payload.get("idempotency_key")
        request_id = (
            key[:12]
            if isinstance(key, str) and IDEMPOTENCY_KEY.fullmatch(key)
            else "invalid"
        )
        runtime = loaded_runtime_data(self._hass)
        if runtime is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        # HA's registries are read here on the event loop; the engine checks
        # membership and scope again under its lock.
        expected = payload.get("expected_rooms")
        ids = list(expected) if isinstance(expected, dict) else []
        if len(ids) > MAX_DEVICE_ENTITIES or any(
            not isinstance(eid, str) for eid in ids
        ):
            return self.json_message("Invalid expected_rooms", HTTPStatus.BAD_REQUEST)
        assignable = {eid for eid in ids if is_assignable(self._hass, eid)}
        fallback = {eid: ha_area_id_of(self._hass, eid) for eid in assignable}
        auth = get_engine(self._hass)
        sub = claims["sub"]
        try:
            actor = await self._hass.async_add_executor_job(
                lambda: auth.member_id_for(sub) if auth else sub
            )
            result = await self._hass.async_add_executor_job(
                lambda: registry.move_device_room(
                    runtime.storage,
                    actor,
                    payload,
                    assignable_ids=assignable,
                    fallback_rooms=fallback,
                    scope=claims.get("rooms"),
                )
            )
        except RoomMoveDenied as err:
            _LOGGER.info("Room move %s rejected: forbidden", request_id)
            return self.json_message(str(err), HTTPStatus.FORBIDDEN)
        except RegistryError as err:
            _LOGGER.info("Room move %s rejected: %s", request_id, type(err).__name__)
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            _LOGGER.warning("Room move %s failed: storage", request_id)
            return self._storage_failure(err)
        _LOGGER.debug(
            "Room move %s acknowledged (replayed=%s)", request_id, result["replayed"]
        )
        if not result["replayed"]:
            self._notify_change("devices")
        return self.json(result)


class CasaSmartDeviceAssignmentView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/devices/{entity_id}.

    PATCH sets the room, display name or sort order (room_id null means
    Unassigned). DELETE drops the record so the entity falls back to its HA
    area.
    """

    url = f"/api/{DOMAIN}/registry/devices/{{entity_id}}"
    name = f"api:{DOMAIN}:registry:device"

    async def patch(self, request: web.Request, entity_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        # is_assignable lets through a device hidden after import, which must
        # stay movable.
        if not is_assignable(self._hass, entity_id):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        scope = claims.get("rooms")
        if scope is not None and not in_scope(self._hass, entity_id, scope):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        assignment, failed = await self._call(
            lambda: registry.assign_device(
                entity_id,
                room_id=payload.get("room_id", ...),
                display_name=payload.get("display_name", ...),
                sort_order=payload.get("sort_order", ...),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("devices")
        return self.json(assignment)

    async def delete(self, request: web.Request, entity_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        scope = claims.get("rooms")
        if scope is not None and not in_scope(self._hass, entity_id, scope):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        _, failed = await self._call(registry.remove_assignment, entity_id)
        if failed is not None:
            return failed
        self._notify_change("devices")
        return self.json({"deleted": entity_id})


class CasaSmartUserDeviceView(_RegistryView):
    """PUT/PATCH/DELETE /api/casasmart/registry/user-devices/{ha_device_id}.

    A physical device grouping entities, with its gangs, config entities,
    type, name and icon. PUT replaces the whole record, PATCH edits fields and
    DELETE releases its entities back to the add-devices list.
    """

    url = f"/api/{DOMAIN}/registry/user-devices/{{ha_device_id}}"
    name = f"api:{DOMAIN}:registry:user-device"

    async def put(self, request: web.Request, ha_device_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        reject = self._scope_reject(
            claims,
            payload.get("control_entity_ids") or payload.get("entity_ids"),
            payload.get("config_entity_ids"),
        )
        if reject is not None:
            return reject
        device, failed = await self._call(
            lambda: registry.upsert_user_device(
                ha_device_id,
                entity_ids=payload.get("entity_ids"),
                control_entity_ids=payload.get("control_entity_ids"),
                gang_types=payload.get("gang_types"),
                gang_names=payload.get("gang_names"),
                gangs=payload.get("gangs"),
                config_entity_ids=payload.get("config_entity_ids"),
                device_type=payload.get("device_type"),
                custom_name=payload.get("custom_name"),
                custom_icon=payload.get("custom_icon"),
                room_id=payload.get("room_id"),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("user-devices")
        return self.json(device)

    async def patch(self, request: web.Request, ha_device_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        reject = self._scope_reject(
            claims,
            payload.get("control_entity_ids") or payload.get("entity_ids"),
            payload.get("config_entity_ids"),
        )
        if reject is not None:
            return reject
        device, failed = await self._call(
            lambda: registry.patch_user_device(
                ha_device_id,
                entity_ids=payload.get("entity_ids", ...),
                control_entity_ids=payload.get("control_entity_ids", ...),
                gang_types=payload.get("gang_types", ...),
                gang_names=payload.get("gang_names", ...),
                gangs=payload.get("gangs", ...),
                config_entity_ids=payload.get("config_entity_ids", ...),
                device_type=payload.get("device_type", ...),
                custom_name=payload.get("custom_name", ...),
                custom_icon=payload.get("custom_icon", ...),
                room_id=payload.get("room_id", ...),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("user-devices")
        return self.json(device)

    async def delete(self, request: web.Request, ha_device_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        # Load the record first: a scoped caller may only delete a device whose
        # entities are all in its rooms.
        existing, failed = await self._call(registry.get_user_device, ha_device_id)
        if failed is not None:
            return failed
        reject = self._scope_reject(
            claims,
            existing.get("entity_ids"),
            existing.get("config_entity_ids"),
        )
        if reject is not None:
            return reject
        _, failed = await self._call(registry.delete_user_device, ha_device_id)
        if failed is not None:
            return failed
        self._notify_change("user-devices")
        return self.json({"deleted": ha_device_id})


class CasaSmartUserDeviceGangView(_RegistryView):
    """PATCH /api/casasmart/registry/user-devices/{ha_device_id}/gangs/{gang}.

    Sets any of presentation, type, name, icon and room_id on one gang. The
    gang path segment is the gang's control entity_id. Only registry metadata
    changes; HA is not touched.
    """

    url = f"/api/{DOMAIN}/registry/user-devices/{{ha_device_id}}/gangs/{{gang}}"
    name = f"api:{DOMAIN}:registry:user-device:gang"

    @staticmethod
    def _apply(
        registry: RegistryEngine,
        ha_device_id: str,
        gang: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply each field in payload; RegistryError if there are none."""
        device = None
        if "presentation" in payload:
            device = registry.set_gang_presentation(
                ha_device_id, gang, payload["presentation"]
            )
        if "type" in payload:
            device = registry.set_gang_type(ha_device_id, gang, payload["type"])
        if "name" in payload or "icon" in payload:
            device = registry.set_gang_name_icon(
                ha_device_id,
                gang,
                name=payload.get("name", ...),
                icon=payload.get("icon", ...),
            )
        if "room_id" in payload:
            device = registry.set_gang_room(ha_device_id, gang, payload["room_id"])
        if device is None:
            raise RegistryError(
                "Body must set presentation, type, name, icon or room_id"
            )
        return device

    async def patch(
        self, request: web.Request, ha_device_id: str, gang: str
    ) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        # The gang's entity must be in the caller's rooms; it needn't be served.
        reject = self._scope_reject(claims, [gang])
        if reject is not None:
            return reject
        device, failed = await self._call(
            lambda: self._apply(registry, ha_device_id, gang, payload)
        )
        if failed is not None:
            return failed
        self._notify_change("user-devices")
        return self.json(device)


class CasaSmartScenesView(_RegistryView):
    """POST /api/casasmart/registry/scenes: create a scene."""

    url = f"/api/{DOMAIN}/registry/scenes"
    name = f"api:{DOMAIN}:registry:scenes"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        if (reject := self._energy_flag_reject(claims, payload)) is not None:
            return reject
        unserved = self._unserved_scene_entity(payload.get("entities"))
        if unserved is not None:
            return unserved
        # Only the user role can be room-scoped and it lacks registry.manage,
        # so this is defense in depth.
        reject = self._scope_reject(claims, _scene_entity_ids(payload.get("entities")))
        if reject is not None:
            return reject
        scene, failed = await self._call(
            lambda: registry.create_scene(
                payload.get("name"),
                payload.get("entities"),
                payload.get("icon"),
                payload.get("works_during_energy_saving", False),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("scenes")
        return self.json(scene, HTTPStatus.CREATED)


class CasaSmartSceneView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/scenes/{scene_id}."""

    url = f"/api/{DOMAIN}/registry/scenes/{{scene_id}}"
    name = f"api:{DOMAIN}:registry:scene"

    async def patch(self, request: web.Request, scene_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        if (reject := self._energy_flag_reject(claims, payload)) is not None:
            return reject
        if "entities" in payload:
            unserved = self._unserved_scene_entity(payload["entities"])
            if unserved is not None:
                return unserved
            reject = self._scope_reject(claims, _scene_entity_ids(payload["entities"]))
            if reject is not None:
                return reject
        scene, failed = await self._call(
            lambda: registry.update_scene(
                scene_id,
                name=payload.get("name", ...),
                entities=payload.get("entities", ...),
                icon=payload.get("icon", ...),
                favorite=payload.get("favorite", ...),
                works_during_energy_saving=payload.get(
                    "works_during_energy_saving", ...
                ),
            )
        )
        if failed is not None:
            return failed
        self._notify_change("scenes")
        return self.json(scene)

    async def delete(self, request: web.Request, scene_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        _, failed = await self._call(registry.delete_scene, scene_id)
        if failed is not None:
            return failed
        self._notify_change("scenes")
        return self.json({"deleted": scene_id})


class CasaSmartSceneActivateView(_RegistryView):
    """POST /api/casasmart/registry/scenes/{scene_id}/activate.

    Each step passes the same whitelist as a single-device command. A failed
    step doesn't stop the rest; the reply has a result per entity.
    """

    url = f"/api/{DOMAIN}/registry/scenes/{{scene_id}}/activate"
    name = f"api:{DOMAIN}:registry:scene:activate"

    async def post(self, request: web.Request, scene_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.control")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        try:
            scene = await self._hass.async_add_executor_job(
                registry.get_scene, scene_id
            )
        except UnknownItemError:
            return self.json_message("Unknown scene", HTTPStatus.NOT_FOUND)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)

        scope = claims.get("rooms")
        if scope is not None and not all(
            in_scope(self._hass, item["entity_id"], scope) for item in scene["entities"]
        ):
            return self.json_message("Unknown scene", HTTPStatus.NOT_FOUND)

        runtime = loaded_runtime_data(self._hass)
        energy = getattr(runtime, "energy", None)
        if energy is not None and energy_lockout_applies(energy, claims):
            return self.json(
                {
                    "error": "energy_lockout",
                    "message": (
                        "Energy saving is active — controls are locked by the admin"
                    ),
                    # The phone reads code on a 403: this isn't an expired login.
                    "code": "energy_lockout",
                },
                HTTPStatus.FORBIDDEN,
            )
        if (
            energy is not None
            and energy.active_level is not None
            and not scene.get("works_during_energy_saving", False)
        ):
            return self.json(
                {
                    "error": "scene_skipped_energy_saving",
                    "scene_id": scene_id,
                    "message": (
                        "Energy Saving is active and this scene isn't set to "
                        "run during it"
                    ),
                },
                HTTPStatus.CONFLICT,
            )

        return self.json(await async_execute_registry_scene(self._hass, scene))


class CasaSmartFavoritesView(_RegistryView):
    """GET/PUT /api/casasmart/me/favorites: the caller's favorite devices.

    Favorites belong to the member, so a member's devices share one list. PUT
    replaces the list in display order.
    """

    url = f"/api/{DOMAIN}/me/favorites"
    name = f"api:{DOMAIN}:me:favorites"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        # A legacy device with no member is its own member.
        engine = get_engine(self._hass)
        sub = claims["sub"]
        scope = claims.get("rooms")

        def _load() -> list[str]:
            return registry.get_favorites(engine.member_id_for(sub) if engine else sub)

        try:
            stored = await self._hass.async_add_executor_job(_load)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        # Filter for the reply only: during HA startup states are still
        # arriving, and saving the filtered list would erase favorites.
        favorites = [
            eid
            for eid in stored
            if self._hass.states.get(eid) is not None
            and is_served(self._hass, eid)
            and in_scope(self._hass, eid, scope)
        ]
        return self.json({"entity_ids": favorites})

    async def put(self, request: web.Request) -> web.Response:
        # session.manage: every session may edit its favorites, a widget may not.
        claims, error = authenticate_request(self._hass, request, "session.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        entity_ids = payload.get("entity_ids")
        if not isinstance(entity_ids, list):
            return self.json_message(
                "entity_ids must be a list", HTTPStatus.BAD_REQUEST
            )
        scope = claims.get("rooms")
        for entity_id in entity_ids:
            if (
                not isinstance(entity_id, str)
                or self._hass.states.get(entity_id) is None
                or not is_served(self._hass, entity_id)
                or not in_scope(self._hass, entity_id, scope)
            ):
                return self.json_message(
                    f"Unknown device {entity_id!r}", HTTPStatus.BAD_REQUEST
                )
        engine = get_engine(self._hass)
        sub = claims["sub"]

        def _load_mid_stored() -> tuple[str, list[str]]:
            mid = engine.member_id_for(sub) if engine else sub
            return mid, registry.get_favorites(mid)

        try:
            member_id, stored = await self._hass.async_add_executor_job(
                _load_mid_stored
            )
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        # A scoped caller only sees its rooms' favorites, so keep the others,
        # after its new list.
        out_of_scope = [eid for eid in stored if not in_scope(self._hass, eid, scope)]
        saved, failed = await self._call(
            registry.set_favorites, member_id, entity_ids + out_of_scope
        )
        if failed is not None:
            return failed
        # The member's other phones re-fetch favorites on registry_changed.
        self._notify_change("favorites")
        return self.json(
            {"entity_ids": [eid for eid in saved if in_scope(self._hass, eid, scope)]}
        )
