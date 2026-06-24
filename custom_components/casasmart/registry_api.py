"""Device-registry endpoints (Track B — B17 hub side).

The REST surface over ``registry.py`` — what replaces every Supabase
room/floor/device/scene/favorites query in the app:

- ``GET /api/casasmart/registry`` — the whole organization in one read:
  floors, rooms, device assignments (with the derived Unassigned pool),
  scenes. Room-scoped tokens get only their slice (``devices.read``).
- ``POST /registry/floors`` + ``PATCH/DELETE .../{floor_id}`` — floor
  CRUD (``registry.manage``: admin + sub-admin; users read-only).
- ``POST /registry/rooms`` + ``PATCH/DELETE .../{room_id}`` — room CRUD.
- ``PATCH/DELETE /registry/devices/{entity_id}`` — assign a device to a
  room / rename / reorder; DELETE drops the record (HA-area fallback).
- ``POST /registry/scenes`` + ``PATCH/DELETE .../{scene_id}`` — scene
  CRUD; every stored command is validated against the entity-bridge
  whitelist at write time.
- ``POST /registry/scenes/{scene_id}/activate`` — run a scene
  (``devices.control``; room-scoped tokens may only activate scenes
  fully inside their scope — anything else is the same 404 as
  nonexistent, no enumeration).
- ``GET/PUT /api/casasmart/me/favorites`` — the caller's own favorites
  (any role; the JWT's ``sub`` is the key, so a token can never touch
  another user's list).

Every mutation fires ``EVENT_REGISTRY_CHANGED`` on the HA bus — the
WS server (B1.5) forwards it to connected apps as a ``registry_changed``
frame so they re-fetch through their own (scoped) GET.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .auth_api import authenticate_request, json_body
from .const import DOMAIN, EVENT_REGISTRY_CHANGED
from .entity_bridge import CommandError, validate_command
from .filtering import area_id_of, ha_area_id_of, in_scope, is_served
from .registry import (
    UNSET,
    InUseError,
    RegistryEngine,
    RegistryError,
    UnknownItemError,
)
from .storage import StorageError

# A single stuck device must not hang a scene activation for minutes.
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


class CasaSmartRegistryView(_RegistryView):
    """GET /api/casasmart/registry — floors, rooms, devices, scenes."""

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

        def _read() -> tuple[list, list, dict, list]:
            return (
                registry.list_floors(),
                registry.list_rooms(),
                registry.list_assignments(),
                registry.list_scenes(),
            )

        try:
            floors, rooms, assignments, scenes = (
                await self._hass.async_add_executor_job(_read)
            )
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)

        # Rooms the registry actually knows. A device can resolve to an
        # HA area that is NOT a registry room (area created after the
        # one-shot import, or a deleted registry room whose HA twin
        # lives on) — report those as Unassigned instead of handing the
        # app a room_id absent from the rooms list.
        known_rooms = {room["room_id"] for room in rooms}

        if scope is not None:
            rooms = [room for room in rooms if room["room_id"] in scope]
        visible_floors = {room["floor_id"] for room in rooms} - {None}
        if scope is not None:
            floors = [
                floor for floor in floors if floor["floor_id"] in visible_floors
            ]

        # Devices = every served entity, with its EFFECTIVE room (registry
        # assignment or HA-area fallback — the same resolution the device
        # list and the WS pushes use). No assignment record + no HA area
        # = the derived Unassigned pool.
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
            # A scene leaks nothing only if EVERYTHING it touches is in scope.
            scenes = [
                scene
                for scene in scenes
                if all(
                    in_scope(self._hass, item["entity_id"], scope)
                    for item in scene["entities"]
                )
            ]

        for collection in (floors, rooms, scenes):
            collection.sort(
                key=lambda item: (item.get("sort_order", 0), item.get("name", ""))
            )

        return self.json(
            {
                "floors": floors,
                "rooms": rooms,
                "devices": devices,
                "scenes": scenes,
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


class CasaSmartDeviceAssignmentView(_RegistryView):
    """PATCH/DELETE /api/casasmart/registry/devices/{entity_id}.

    PATCH assigns/renames/reorders; ``room_id: null`` pins the entity to
    the Unassigned pool. DELETE drops the record — the entity reverts to
    HA's own area.
    """

    url = f"/api/{DOMAIN}/registry/devices/{{entity_id}}"
    name = f"api:{DOMAIN}:registry:device"

    async def patch(self, request: web.Request, entity_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
        if self._hass.states.get(entity_id) is None or not is_served(
            self._hass, entity_id
        ):
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
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        registry, not_ready = self._registry_or_503()
        if not_ready is not None:
            return not_ready
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


class CasaSmartScenesView(_RegistryView):
    """POST /api/casasmart/registry/scenes — create a scene."""

    url = f"/api/{DOMAIN}/registry/scenes"
    name = f"api:{DOMAIN}:registry:scenes"

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
        unserved = self._unserved_scene_entity(payload.get("entities"))
        if unserved is not None:
            return unserved
        try:
            scene = await self._hass.async_add_executor_job(
                lambda: registry.create_scene(
                    payload.get("name"),
                    payload.get("entities"),
                    payload.get("icon"),
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
        if "entities" in payload:
            unserved = self._unserved_scene_entity(payload["entities"])
            if unserved is not None:
                return unserved
        try:
            scene = await self._hass.async_add_executor_job(
                lambda: registry.update_scene(
                    scene_id,
                    name=payload.get("name", ...),
                    entities=payload.get("entities", ...),
                    icon=payload.get("icon", ...),
                    favorite=payload.get("favorite", ...),
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

        # IR-AC fix: a climate "on" scene action is stored as a state command
        # (set_temperature/set_hvac_mode) PLUS a separate set_fan_mode. For ACs
        # driven over IR (Broadlink/SmartIR) every climate.* call is a full-state
        # IR burst, so firing both back-to-back sends two codes and the second
        # collides with the AC mid-power-on (it beeps and never settles on).
        # Send ONE command per AC: drop the redundant set_fan_mode when the same
        # climate entity already carries a state command in this scene. (Mirrors
        # HA's scene.turn_on, which only re-sends attributes that actually differ;
        # the set_temperature alone is one clean IR that turns the AC on.)
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
            )
        ]

        results = []
        for item in entities_to_run:
            entity_id = item["entity_id"]
            # Same liveness gate as the single command endpoint — an
            # entity hidden AFTER the scene was written must not stay
            # controllable through it.
            if self._hass.states.get(entity_id) is None or not is_served(
                self._hass, entity_id
            ):
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
                    self._hass.services.async_call(
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
                    "Scene %s: %s on %s timed out", scene_id, item["action"],
                    entity_id,
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

        return self.json(
            {
                "scene_id": scene_id,
                "ok": all(result["ok"] for result in results),
                "results": results,
            }
        )


class CasaSmartFavoritesView(_RegistryView):
    """GET/PUT /api/casasmart/me/favorites — the caller's own list.

    Keyed by the JWT's ``sub`` (the enrolled device id) — the only user
    identity the hub has. PUT replaces the whole list; order is the
    display order. The ONE source of truth replacing the app's three
    (plan B17, audit 2026-06-10).
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
        try:
            favorites = await self._hass.async_add_executor_job(
                registry.get_favorites, claims["sub"]
            )
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
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
        try:
            saved = await self._hass.async_add_executor_job(
                registry.set_favorites, claims["sub"], entity_ids
            )
        except RegistryError as err:
            return self._error_response(err)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        return self.json({"entity_ids": saved})
