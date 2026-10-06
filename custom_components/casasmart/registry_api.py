
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .auth_api import authenticate_request, get_engine, json_body
from .auth_engine import AuthEngine
from .const import DOMAIN, EVENT_REGISTRY_CHANGED
from .entity_bridge import CommandError, validate_command
from .energy_runtime import energy_lockout_applies
from .filtering import area_id_of, ha_area_id_of, in_scope, is_assignable, is_served
from .registry import (
    UNSET,
    InUseError,
    RegistryEngine,
    RegistryError,
    UnknownItemError,
    RoomMoveDenied,
)
from .storage import StorageError


_SCENE_CALL_TIMEOUT = 10.0

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def get_registry(hass: HomeAssistant) -> RegistryEngine | None:
    """The loaded entry's registry engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.registry


def _runtime_data(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


class _RegistryView(HomeAssistantView):
    """Shared plumbing for the registry views below."""

    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _registry_or_503(
        self,
    ) -> tuple[RegistryEngine | None, web.Response | None]:
        registry = get_registry(self._hass)
        if registry is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return registry, None

    def _notify_change(self, kind: str) -> None:
        """Tell connected apps the organization changed (they re-fetch)."""
        self._hass.bus.async_fire(EVENT_REGISTRY_CHANGED, {"kind": kind})

    def _error_response(self, err: RegistryError) -> web.Response:
        if isinstance(err, UnknownItemError):
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        if isinstance(err, InUseError):
            return self.json_message(str(err), HTTPStatus.CONFLICT)
        return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

    def _storage_failure(self, err: Exception) -> web.Response:
        """SQLite trouble is a hub problem, not a client one — log the
        cause, hand the app a clean 500 instead of an aiohttp stack."""
        _LOGGER.error("Registry storage failure: %s", err)
        return self.json_message(
            "Storage failure", HTTPStatus.INTERNAL_SERVER_ERROR
        )

    def _energy_flag_reject(
        self, claims: dict[str, Any], payload: dict[str, Any]
    ) -> web.Response | None:
        if (
            "works_during_energy_saving" in payload
            and not AuthEngine.authorize(claims, "energy.manage")
        ):
            return self.json_message(
                "Energy Saving flags require admin access",
                HTTPStatus.FORBIDDEN,
            )
        return None

    def _unserved_scene_entity(self, entities: Any) -> web.Response | None:
        """The is_served gate for scene WRITES — same rule as the single
        command endpoint: a scene must not be able to store a command
        against an entity the app can't target directly (the one-filter
        doctrine in ``filtering``). Shape errors fall through — the
        engine 400s those with a precise message."""
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
        """Reject a write touching an entity outside the caller's scope — same
        no-enumeration message as the favorites/assignment writes. An admin
        (rooms=None) passes the scope test. Assignability is NOT checked
        (Phase 9): a gang's type is presentation metadata, so a grouped/typed
        record must stay writable even when its underlying entities aren't
        individually servable (a dead/hidden relay, an all-dead device on DELETE,
        a presentation flip that writes no HA entity). Returns an error response,
        or None to proceed."""
        scope = claims.get("rooms")
        for entity_ids in entity_lists:
            if not isinstance(entity_ids, list):
                continue
            for entity_id in entity_ids:
                if (
                    not isinstance(entity_id, str)
                    or not in_scope(self._hass, entity_id, scope)
                ):
                    return self.json_message(
                        f"Unknown device {entity_id!r}", HTTPStatus.BAD_REQUEST
                    )
        return None


class CasaSmartRegistryView(_RegistryView):

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

        def _read() -> tuple[list, list, list, dict, list]:
            return (
                registry.list_floors(),
                registry.list_rooms(),
                registry.list_room_tags(),
                registry.list_assignments(),
                registry.list_scenes(),
            )

        try:
            floors, rooms, room_tags, assignments, scenes = (
                await self._hass.async_add_executor_job(_read)
            )
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
            floors = [
                floor for floor in floors if floor["floor_id"] in visible_floors
            ]





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

        user_devices = registry.list_user_devices()




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
    """POST /api/casasmart/registry/floors — create a floor."""

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
        try:
            floor = await self._hass.async_add_executor_job(
                lambda: registry.create_floor(
                    payload.get("name"), payload.get("sort_order")
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
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
        try:
            floor = await self._hass.async_add_executor_job(
                lambda: registry.update_floor(
                    floor_id,
                    payload.get("name", ...),
                    payload.get("sort_order", ...),
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("floors")
        return self.json(floor)

    async def delete(self, request: web.Request, floor_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        try:
            await self._hass.async_add_executor_job(
                registry.delete_floor, floor_id
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("floors")
        return self.json({"deleted": floor_id})


class CasaSmartRoomsView(_RegistryView):
    """POST /api/casasmart/registry/rooms — create a room."""

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
        try:
            room = await self._hass.async_add_executor_job(
                lambda: registry.create_room(
                    payload.get("name"),
                    payload.get("floor_id"),
                    payload.get("icon"),
                    payload.get("sort_order"),
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
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
        try:
            room = await self._hass.async_add_executor_job(
                lambda: registry.update_room(
                    room_id,
                    name=payload.get("name", ...),
                    floor_id=payload.get("floor_id", ...),
                    icon=payload.get("icon", ...),
                    sort_order=payload.get("sort_order", ...),
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("rooms")
        return self.json(room)

    async def delete(self, request: web.Request, room_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        # Imported rooms share their id with an HA area that lives on
        # after this delete. Entities with no registry record would
        # quietly fall back to it — the room would be dead in the app
        # but alive for room-scoped tokens. Pin every such entity to an
        # explicit Unassigned so deleting a room MEANS it (event-loop
        # reads only; the writes happen in the executor job below).
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

        try:
            unassigned = await self._hass.async_add_executor_job(_delete)
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("rooms")
        return self.json({"deleted": room_id, "devices_unassigned": unassigned})


class CasaSmartRoomTagsView(_RegistryView):
    """POST /api/casasmart/registry/tags — create a multi-room tag."""

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
        try:
            tag = await self._hass.async_add_executor_job(
                registry.create_room_tag,
                payload.get("name"),
                payload.get("color"),
                payload.get("room_ids"),
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
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
        try:
            tag = await self._hass.async_add_executor_job(
                lambda: registry.update_room_tag(
                    tag_id,
                    name=payload.get("name", ...),
                    color=payload.get("color", ...),
                    room_ids=payload.get("room_ids", ...),
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("room-tags")
        return self.json(tag)

    async def delete(self, request: web.Request, tag_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        try:
            await self._hass.async_add_executor_job(registry.delete_room_tag, tag_id)
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("room-tags")
        return self.json({"deleted": tag_id})


class CasaSmartRoomMoveView(_RegistryView):
    """Atomic, compare-and-set logical room move; never controls hardware."""

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
            return self.json_message("Body must be a JSON object", HTTPStatus.BAD_REQUEST)
        key = payload.get("idempotency_key")
        request_id = key[:12] if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key) else "invalid"
        runtime = _runtime_data(self._hass)
        if runtime is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        # HA registry/state APIs are read on the event loop. The engine checks
        # exact saved membership and room scope again under its lock.
        expected = payload.get("expected_rooms")
        ids = list(expected) if isinstance(expected, dict) else []
        if len(ids) > 100 or any(not isinstance(eid, str) for eid in ids):
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
                    runtime.storage, actor, payload, assignable_ids=assignable,
                    fallback_rooms=fallback, scope=claims.get("rooms"),
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
        _LOGGER.debug("Room move %s acknowledged (replayed=%s)", request_id, result["replayed"])
        if not result["replayed"]:
            self._notify_change("devices")
        return self.json(result)


class CasaSmartDeviceAssignmentView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/devices/{entity_id}.

    PATCH assigns/renames/reorders; ``room_id: null`` pins the entity to
    the Unassigned pool. DELETE drops the record — the entity reverts to
    HA's own area.
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
        # ORGANIZE surface (not the feed): a device the integration has since
        # hidden (e.g. a secondary gang switch, hidden_by='integration') must
        # still be re-assignable/clearable. is_served would 404 it — see
        # is_assignable.
        if not is_assignable(self._hass, entity_id):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        # A room-scoped caller may only (re)assign entities inside its own
        # rooms — mirror the in_scope guard every sibling write enforces, so a
        # scoped sub-admin can't move a device in a room it doesn't own.
        scope = claims.get("rooms") if claims else None
        if scope is not None and not in_scope(self._hass, entity_id, scope):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            assignment = await self._hass.async_add_executor_job(
                lambda: registry.assign_device(
                    entity_id,
                    room_id=payload.get("room_id", ...),
                    display_name=payload.get("display_name", ...),
                    sort_order=payload.get("sort_order", ...),
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("devices")
        return self.json(assignment)

    async def delete(self, request: web.Request, entity_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        scope = claims.get("rooms") if claims else None
        if scope is not None and not in_scope(self._hass, entity_id, scope):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        try:
            await self._hass.async_add_executor_job(
                registry.remove_assignment, entity_id
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("devices")
        return self.json({"deleted": entity_id})


class CasaSmartUserDeviceView(_RegistryView):
    """PUT/PATCH/DELETE /api/casasmart/registry/user-devices/{ha_device_id}.

    The grouped physical-device record — gang types/names, config entities,
    device type, custom name/icon. PUT upserts the whole record (import /
    re-import); PATCH edits named fields (rename, re-type a gang, set an icon);
    DELETE un-grabs the device so its entities return to the add-devices list.
    """

    url = f"/api/{DOMAIN}/registry/user-devices/{{ha_device_id}}"
    name = f"api:{DOMAIN}:registry:user-device"

    async def put(
        self, request: web.Request, ha_device_id: str
    ) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
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
        try:
            device = await self._hass.async_add_executor_job(
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
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("user-devices")
        return self.json(device)

    async def patch(
        self, request: web.Request, ha_device_id: str
    ) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
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
        try:
            device = await self._hass.async_add_executor_job(
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
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("user-devices")
        return self.json(device)

    async def delete(
        self, request: web.Request, ha_device_id: str
    ) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        # A scoped caller may only un-grab a device whose entities are all in
        # its scope; load the record first so an out-of-scope delete is refused
        # (admin passes). Assignability isn't checked — a dead-entity device must
        # stay removable.
        try:
            existing = await self._hass.async_add_executor_job(
                registry.get_user_device, ha_device_id
            )
        except RegistryError as err:
            return self._error_response(err)
        reject = self._scope_reject(
            claims,
            existing.get("entity_ids"),
            existing.get("config_entity_ids"),
        )
        if reject is not None:
            return reject
        try:
            await self._hass.async_add_executor_job(
                registry.delete_user_device, ha_device_id
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("user-devices")
        return self.json({"deleted": ha_device_id})


class CasaSmartUserDeviceGangView(_RegistryView):
    """PATCH /api/casasmart/registry/user-devices/{ha_device_id}/gangs/{gang}.

    Flip ONE gang's presentation model — promote/hide/un-hide (``presentation``),
    re-type (``type``), or relabel (``name``/``icon``). Pure presentation
    metadata: it never touches the device's entities or HA. The body carries any
    of ``{presentation, type, name, icon}`` and each provided field is applied;
    the exactly-one-of {grouped, solo, hidden} invariant is the engine's. The
    gang path segment IS the gang's control entity_id.
    """

    url = (
        f"/api/{DOMAIN}/registry/user-devices/{{ha_device_id}}/gangs/{{gang}}"
    )
    name = f"api:{DOMAIN}:registry:user-device:gang"

    @staticmethod
    def _apply(
        registry: RegistryEngine,
        ha_device_id: str,
        gang: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply each provided field via its dedicated engine command. An empty
        body (no presentation/type/name/icon) is a 400 — a no-op PATCH is a
        client mistake, not a silent success."""
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
            device = registry.set_gang_room(
                ha_device_id, gang, payload["room_id"]
            )
        if device is None:
            raise RegistryError(
                "Body must set presentation, type, name, icon or room_id"
            )
        return device

    async def patch(
        self, request: web.Request, ha_device_id: str, gang: str
    ) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
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
        # A scoped caller may only flip a gang whose control entity_id is in its
        # scope (admin passes). Assignability isn't checked — a presentation flip
        # writes no HA entity, so a hidden/dead relay must stay flippable.
        reject = self._scope_reject(claims, [gang])
        if reject is not None:
            return reject
        try:
            device = await self._hass.async_add_executor_job(
                lambda: self._apply(registry, ha_device_id, gang, payload)
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("user-devices")
        return self.json(device)


def _scene_entity_ids(entities: Any) -> list[str]:
    """The entity_ids referenced by a scene's entities payload (str ids only)."""
    if not isinstance(entities, list):
        return []
    return [
        e["entity_id"]
        for e in entities
        if isinstance(e, dict) and isinstance(e.get("entity_id"), str)
    ]


class CasaSmartScenesView(_RegistryView):
    """POST /api/casasmart/registry/scenes — create a scene."""

    url = f"/api/{DOMAIN}/registry/scenes"
    name = f"api:{DOMAIN}:registry:scenes"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
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
        # Scope re-check — the same defense-in-depth the favorites/user-device
        # writes do. Unreachable today (every registry.manage caller is unscoped),
        # but consistent if a scoped non-user role is ever introduced.
        reject = self._scope_reject(
            claims, _scene_entity_ids(payload.get("entities"))
        )
        if reject is not None:
            return reject
        try:
            scene = await self._hass.async_add_executor_job(
                lambda: registry.create_scene(
                    payload.get("name"),
                    payload.get("entities"),
                    payload.get("icon"),
                    payload.get("works_during_energy_saving", False),
                )
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("scenes")
        return self.json(scene, HTTPStatus.CREATED)


class CasaSmartSceneView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/scenes/{scene_id}."""

    url = f"/api/{DOMAIN}/registry/scenes/{{scene_id}}"
    name = f"api:{DOMAIN}:registry:scene"

    async def patch(self, request: web.Request, scene_id: str) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
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
            reject = self._scope_reject(
                claims, _scene_entity_ids(payload["entities"])
            )
            if reject is not None:
                return reject
        try:
            scene = await self._hass.async_add_executor_job(
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
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("scenes")
        return self.json(scene)

    async def delete(self, request: web.Request, scene_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        try:
            await self._hass.async_add_executor_job(
                registry.delete_scene, scene_id
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        self._notify_change("scenes")
        return self.json({"deleted": scene_id})


class CasaSmartSceneActivateView(_RegistryView):
    """POST /api/casasmart/registry/scenes/{scene_id}/activate.

    Runs every command in the scene through the SAME whitelist validation
    as the single-device command endpoint. Partial failures don't abort
    the rest — the response reports per-entity results ("turn the house
    to movie mode" shouldn't die because one bulb is unplugged).
    """

    url = f"/api/{DOMAIN}/registry/scenes/{{scene_id}}/activate"
    name = f"api:{DOMAIN}:registry:scene:activate"

    async def post(self, request: web.Request, scene_id: str) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "devices.control"
        )
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
            in_scope(self._hass, item["entity_id"], scope)
            for item in scene["entities"]
        ):
            # Same 404 as nonexistent — out-of-scope scenes are invisible.
            return self.json_message("Unknown scene", HTTPStatus.NOT_FOUND)

        runtime = _runtime_data(self._hass)
        energy = getattr(runtime, "energy", None)
        if energy is not None and energy_lockout_applies(energy, claims):
            return self.json(
                {
                    "error": "energy_lockout",
                    "message": (
                        "Energy saving is active — controls are locked "
                        "by the admin"
                    ),
                },
                HTTPStatus.FORBIDDEN,
            )
        if (
            energy is not None
            and energy.active_level is not None
            and not scene.get("works_during_energy_saving", False)
        ):
            return self.json(
                {"error": "scene_skipped_energy_saving", "scene_id": scene_id},
                HTTPStatus.CONFLICT,
            )

        return self.json(await async_execute_registry_scene(self._hass, scene))


async def async_execute_registry_scene(
    hass: HomeAssistant, scene: dict[str, Any]
) -> dict[str, Any]:
    scene_id = scene["scene_id"]










    _climate_with_state = {
        item["entity_id"]
        for item in scene["entities"]
        if item["entity_id"].split(".", 1)[0] == "climate"
        and item.get("action") in ("set_temperature", "set_hvac_mode")
    }
    entities_to_run = [
        item
        for item in scene["entities"]
        if not (
            item["entity_id"].split(".", 1)[0] == "climate"
            and item.get("action") == "set_fan_mode"
            and item["entity_id"] in _climate_with_state
            and not scene.get("generated_room_v1", False)
        )
    ]

    results = []
    for item in entities_to_run:
        entity_id = item["entity_id"]

        if scene.get("generated_room_v1"):
            # Each service call yields: a room move or another scene can occur
            # between commands. Never brighten/re-enable an interveningly off
            # light, cool harder, or continue controlling a moved device.
            from .generated_suggestions import room_actions

            state = hass.states.get(entity_id)
            valid = state is not None and area_id_of(hass, entity_id) == scene["room_id"]
            if valid and entity_id.startswith("light."):
                valid = (
                    state.state == "on"
                    and state.attributes.get("casasmart_room_activity_exclude") is not True
                )
                if valid and item["action"] == "turn_on":
                    valid = any(
                        a["action"] == item["action"] and a["data"] == item.get("data", {})
                        for a in room_actions([state], "room_eco")
                    )
            elif valid:
                unit = getattr(getattr(hass.config, "units", None), "temperature_unit", "°C")
                valid = any(
                    a["action"] == item["action"] and a["data"] == item.get("data", {})
                    for a in room_actions([state], scene["kind"], temperature_unit=unit)
                )
            if not valid:
                results.append({
                    "entity_id": entity_id,
                    "ok": False,
                    "error": "Device changed since preview",
                })
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
            results.append(
                {"entity_id": entity_id, "ok": False, "error": "Timed out"}
            )
        except (CommandError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Scene %s: %s on %s failed: %s",
                scene_id,
                item["action"],
                entity_id,
                err,
            )
            results.append(
                {"entity_id": entity_id, "ok": False, "error": str(err)}
            )

    return {
        "scene_id": scene_id,
        "ok": all(result["ok"] for result in results),
        "results": results,
    }


class CasaSmartFavoritesView(_RegistryView):
    """GET/PUT /api/casasmart/me/favorites — the caller's own list.

    Keyed by ``member_id`` (the PERSON, resolved from the request's device
    ``sub`` — Phase 5), so a member's devices share one list. PUT replaces the
    whole list; order is the display order. GET drops gone/unserved + out-of-
    scope ids for the response without mutating storage — the ONE source of
    truth for device favorites (plan B17). A GET must remain read-only because
    integrations are still populating HA states while the hub starts; treating
    a temporarily absent state as a permanent deletion erased favorites during
    host restarts.
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
        # Favorites roam per PERSON: resolve the request's device sub to its
        # member_id (Phase 5) inside the executor — a legacy device is its own
        # one-device member, so it falls back to the sub.
        engine = get_engine(self._hass)
        sub = claims["sub"]
        scope = claims.get("rooms")

        def _load() -> tuple[str, list[str]]:
            mid = engine.member_id_for(sub) if engine else sub
            return mid, registry.get_favorites(mid)

        try:
            member_id, stored = await self._hass.async_add_executor_job(_load)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        # Do not paint a currently absent/unserved entity, but never persist
        # that filtering from a GET. During HA startup an integration may not
        # have populated its states yet; a read-time "self-heal" would mistake
        # that normal transient for deletion and permanently wipe favorites.
        # An explicit PUT remains the authoritative cleanup/edit path.
        served = [
            eid
            for eid in stored
            if self._hass.states.get(eid) is not None
            and is_served(self._hass, eid)
        ]
        # Scope to the caller — a room-scoped user only sees its own rooms.
        favorites = [eid for eid in served if in_scope(self._hass, eid, scope)]
        return self.json({"entity_ids": favorites})

    async def put(self, request: web.Request) -> web.Response:
        # devices.control, not devices.read: it's a write. Every current
        # role may favorite, but a future read-only role must not slip
        # through a read permission into a mutation.
        claims, error = authenticate_request(self._hass, request, "devices.control")
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
                # Same wording for unknown and out-of-scope — no enumeration.
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
        # A room-scoped caller only SEES + submits its in-scope slice (the GET
        # filters out-of-scope). A plain replace would silently delete the
        # member's favorites in rooms it can't see (Phase 7) — preserve those,
        # appended after the caller's now-authoritative in-scope list.
        out_of_scope = [
            eid for eid in stored if not in_scope(self._hass, eid, scope)
        ]
        try:
            saved = await self._hass.async_add_executor_job(
                registry.set_favorites, member_id, entity_ids + out_of_scope
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        # Nudge the member's other devices to re-pull (Phase 5). The app
        # re-fetches favorites + settings on any registry_changed, so under
        # per-account keying the shared change lands on the sibling phone.
        self._notify_change("favorites")
        # Echo only the caller's in-scope view — never leak out-of-scope ids.
        return self.json(
            {
                "entity_ids": [
                    eid for eid in saved if in_scope(self._hass, eid, scope)
                ]
            }
        )
