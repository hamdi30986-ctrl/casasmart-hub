"""Now page endpoints: the page snapshot and room activity commands.

- GET /api/casasmart/now (devices.read): the caller's Now page in one
  response (recent devices, scenes, room activity, weather, air quality,
  doors and windows), filtered to their room scope. ?habits=household ranks
  the off rooms on the household's habits (the shared tablet); the default,
  personal, on the caller's own blended over them.
- GET/PUT /api/casasmart/now/config (registry.manage): the sources the page
  shows. The hub never picks sensors by itself.
- PUT /api/casasmart/now/rooms/{room_id}/activity-policy (registry.manage):
  whether a room takes part in room commands, and which devices they switch.
  A room without one takes part with its lights and fans.
- GET/PUT/DELETE /api/casasmart/now/rooms/{room_id}/start-profile
  (registry.manage): the lights and fans one tap "on" turns on when nothing
  better is known.
- POST /api/casasmart/now/rooms/{room_id}/activity (devices.control): switch
  a room's approved devices off, or its lights and fans back on: what the
  hub's last off captured, else what was on before the room last went dark,
  else the room's start profile (room_intel.one_tap; 409 nothing_to_turn_on
  when there is nothing, and the app opens the room). Idempotent per client
  key, one command per room at a time.
- GET/PUT /api/casasmart/now/habits and POST /api/casasmart/now/habits/reset
  (users.manage, admin only): room habit learning, which the all-off
  ranking uses (room_intel.learner).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .auth_api import (
    async_member_id,
    authenticate_request,
    read_json_object,
    ready_or_503,
)
from .const import DOMAIN
from .energy_runtime import energy_lockout_applies, energy_lockout_refusal
from .filtering import area_id_of, in_scope, is_served, serialize_device, visible_to
from .now_data import (
    MAX_START_PROFILE,
    NowDataEngine,
    NowDataError,
    is_default_room_device,
    is_room_activity_candidate,
    is_room_activity_eligible,
    lighting_kind,
    room_activity_layout,
    summarize_openings,
)
from .registry import RegistryEngine
from .room_intel.activity import SOURCE_APP
from .room_intel.habits import (
    LAYER_HOUSEHOLD,
    LAYERS,
    STATUS_UNAVAILABLE,
    HabitSettingsError,
)
from .room_intel.one_tap import (
    RELAY_DOMAIN,
    SOURCE_CAPTURE,
    RoomOnPlan,
    capture_counts,
    is_on_domain,
    one_tap_summary,
    plan_room,
    present_ids,
)
from .room_intel.ranking import (
    async_counted_controls,
    async_rank_rooms,
    imported_controls,
    ranking_summary,
)
from .runtime_lookup import command_context, loaded_runtime_data
from .storage import StorageError

if TYPE_CHECKING:
    from homeassistant.core import Context

_LOGGER = logging.getLogger(__name__)


_STALE_SECONDS = 60 * 60
_AIR_QUALITY_DEVICE_CLASSES = frozenset(
    {
        "aqi",
        "carbon_dioxide",
        "carbon_monoxide",
        "nitrogen_dioxide",
        "ozone",
        "pm1",
        "pm25",
        "pm10",
        "volatile_organic_compounds",
        "volatile_organic_compounds_parts",
    }
)
_CONTACT_DEVICE_CLASSES = frozenset({"door", "window", "opening"})
# The 409 nothing_to_turn_on's message, by reason (room_intel.one_tap).
_NOTHING_TO_TURN_ON = {
    "nothing_saved": "Nothing to turn on: the hub doesn't know which lights "
    "and fans this room had on, and it has no start profile",
    "unavailable": "Nothing to turn on: this room's lights and fans are unavailable",
}


# -- helpers ------------------------------------------------------------------


def get_now_data(hass: HomeAssistant) -> NowDataEngine | None:
    """The Now store of the loaded entry, or None while the hub isn't loaded."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.now_data if runtime_data is not None else None


def _room_command_locks(hass: HomeAssistant) -> dict[str, asyncio.Lock]:
    """The per-room command locks, kept in hass.data.

    build_views makes separate view objects for HA's HTTP app and the TLS
    listener, and a room's commands must queue whichever one they arrive on.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault("room_command_locks", {})


def _approved(
    state: Any, policy: dict[str, Any], gang_types: Mapping[str, str | None]
) -> bool:
    """True when the room's policy lets room commands switch this device.

    A stored policy approves the candidates on its list. A room without one
    takes part with its default devices: its lights and fans imported into
    the registry, wall relays the installer typed as one included
    (now_data.is_default_room_device). gang_types are the registry's
    imported controls with their gang types (ranking.imported_controls). The
    caller checks the device is in the room and served.
    """
    entity_id = state.entity_id
    if policy["default"]:
        return entity_id in gang_types and is_default_room_device(
            state, gang_types[entity_id]
        )
    return is_room_activity_eligible(
        state, policy["eligible_entity_ids"], gang_types.get(entity_id)
    )


def _one_tap_devices(
    hass: HomeAssistant,
    policies: dict[str, dict[str, Any]],
    gang_types: Mapping[str, str | None],
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Each room's devices one tap "on" may switch, by entity id, with their
    state, and each one's kind ("light" or "fan").

    policies are the rooms that take part in room commands, with their
    policy. A device counts in its room when the policy approves it
    (_approved), it is served, and it is a light or fan to the hub
    (now_data.lighting_kind: a wall relay the installer typed as one too).
    One scan.
    """
    devices: dict[str, dict[str, str]] = {room_id: {} for room_id in policies}
    kinds: dict[str, str] = {}
    for state in hass.states.async_all():
        entity_id = state.entity_id
        kind = lighting_kind(state, gang_types.get(entity_id))
        if kind is None:
            continue
        room_id = area_id_of(hass, entity_id)
        if room_id not in policies:
            continue
        if _approved(state, policies[room_id], gang_types) and is_served(
            hass, entity_id
        ):
            devices[room_id][entity_id] = state.state
            kinds[entity_id] = kind
    return devices, kinds


def _last_on(hass: HomeAssistant, room_id: str) -> Any:
    """The room's last-on set (the recorder's), None without one."""
    recorder = getattr(loaded_runtime_data(hass), "room_activity", None)
    return recorder.last_on.get(room_id) if recorder is not None else None


def _state_is_unavailable(state: Any) -> bool:
    return state is None or state.state in {"unknown", "unavailable"}


def _state_stale(state: Any) -> bool:
    """True when an available state has not been updated for an hour."""
    if _state_is_unavailable(state):
        return False
    changed = getattr(state, "last_updated", None) or getattr(
        state, "last_changed", None
    )
    if not isinstance(changed, datetime):
        return False
    return (
        datetime.now(UTC) - changed.astimezone(UTC)
    ).total_seconds() > _STALE_SECONDS


# -- views --------------------------------------------------------------------


class _NowView(HomeAssistantView):
    """Shared plumbing for the Now views."""

    requires_auth = False  # CasaSmart JWT gate

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _now_or_503(self) -> tuple[NowDataEngine | None, web.Response | None]:
        """The Now store, or a 503 while the hub isn't loaded."""
        return ready_or_503(self, get_now_data(self._hass))

    def _registry(self) -> RegistryEngine | None:
        data = loaded_runtime_data(self._hass)
        return data.registry if data is not None else None

    def _storage_failure(self, err: Exception) -> web.Response:
        """Log a storage error and answer a clean 500."""
        _LOGGER.error("Now storage failure: %s", err)
        return self.json_message("Storage failure", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _device_class(self, entity_id: str, state: Any) -> Any:
        """The integration's own device class, else the one in the state."""
        entry = er.async_get(self._hass).async_get(entity_id)
        if entry is not None and entry.original_device_class:
            return entry.original_device_class
        return state.attributes.get("device_class")


class CasaSmartNowView(_NowView):
    """Return the server-computed, user-scoped Now snapshot."""

    url = f"/api/{DOMAIN}/now"
    name = f"api:{DOMAIN}:now"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        layer = request.query.get("habits", "personal")
        if layer not in LAYERS:
            return self.json_message(
                "habits must be household or personal", HTTPStatus.BAD_REQUEST
            )
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        registry = self._registry()
        if registry is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            member_id = await async_member_id(self._hass, claims)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        scope = claims.get("rooms")

        def read_stores() -> tuple:
            recents = now_data.recents_for(member_id)
            config = now_data.config()
            contacts_picked = now_data.contacts_picked()
            rooms = registry.list_rooms()
            scenes = registry.list_scenes()
            favorites = registry.get_favorites(member_id)
            policies = {
                room["room_id"]: now_data.room_policy(room["room_id"]) for room in rooms
            }
            return (
                recents,
                config,
                contacts_picked,
                rooms,
                scenes,
                favorites,
                policies,
                registry.list_user_devices(),
            )

        (
            recents,
            config,
            contacts_picked,
            all_rooms,
            scenes,
            favorites,
            policies,
            devices,
        ) = await self._hass.async_add_executor_job(read_stores)
        rooms = [
            room for room in all_rooms if scope is None or room["room_id"] in scope
        ]
        room_ids = {room["room_id"] for room in rooms}

        recent_items = [
            entry
            for item in recents
            if (entry := self._recent_entry(item["entity_id"], item["at"], claims))
        ]
        recent_source = "recency"
        if not recent_items:
            recent_source = "favorites_seed"
            recent_items = [
                entry
                for entity_id in favorites
                if (entry := self._recent_entry(entity_id, None, claims))
            ]

        # The generated suggestions rank rooms with the same function, so both
        # agree on the busiest rooms. The room activity log orders the off
        # rooms that just went dark (the caller's own offs after the habits),
        # and the habits the ones usually on around now: the household's for
        # the shared tablet, the caller's own blended over them for a phone.
        # The household's view has no "you": on a shared screen everyone's
        # offs are just offs.
        household = layer == LAYER_HOUSEHOLD
        learner = getattr(loaded_runtime_data(self._hass), "room_habits", None)
        habits = (
            learner.query(None if household else member_id)
            if learner is not None
            else None
        )
        ranked, _, _ = await async_rank_rooms(
            self._hass,
            registry,
            now_data,
            scope,
            activity=getattr(loaded_runtime_data(self._hass), "room_activity", None),
            member_id=None if household else member_id,
            habits=habits,
            # Read above, in the same executor job.
            stores=(all_rooms, policies, devices),
        )

        # What one tap "on" would do in each room that takes part in room
        # commands (the room command's own plan), so the apps show the ON
        # button only when it turns something on. restore_pending_count is
        # how many devices it would turn on, for apps that only read that.
        # A room without a stored policy takes part with its lights and fans.
        approved = {
            room["room_id"]: policies[room["room_id"]]
            for room in ranked
            if policies.get(room["room_id"], {}).get("participates", False)
        }

        def read_one_tap_sources() -> dict[str, tuple]:
            return {
                room_id: (
                    now_data.restore_capture(room_id),
                    now_data.start_profile(room_id),
                )
                for room_id in approved
            }

        one_tap_sources = await self._hass.async_add_executor_job(read_one_tap_sources)
        devices_by_room, kinds = _one_tap_devices(
            self._hass, approved, imported_controls(devices)
        )
        now_t = time.time()
        active_rooms = []
        for room in ranked:
            room_id = room["room_id"]
            if room_id in approved:
                capture, start_profile = one_tap_sources[room_id]
                plan = plan_room(
                    capture=capture,
                    last_on=_last_on(self._hass, room_id),
                    start_profile=start_profile,
                    devices=devices_by_room[room_id],
                    now_t=now_t,
                    kinds=kinds,
                )
                one_tap = one_tap_summary(plan, kinds)
                pending = len(plan.targets)
            else:
                one_tap = {"action": "open_room", "reason": "room_activity_disabled"}
                pending = 0
            active_rooms.append(
                {
                    **room,
                    "restore_pending_count": pending,
                    "rank": {**room["rank"], "one_tap": one_tap},
                }
            )

        scenes_by_id = {scene["scene_id"]: scene for scene in scenes}

        def visible_scene(scene_id: str | None) -> dict[str, Any] | None:
            scene = scenes_by_id.get(scene_id) if scene_id else None
            if scene is None or not all(
                in_scope(self._hass, item["entity_id"], scope)
                for item in scene["entities"]
            ):
                return None
            return {key: scene[key] for key in ("scene_id", "name", "icon")}

        weather = self._weather_payload(config.get("outdoor_weather_entity_id"))
        air_quality = self._air_quality_payload(config.get("air_quality_entity_id"))
        # Until an admin saves a list, every door/window sensor counts, as in
        # Apple Home; a home without a tablet never gets a picker.
        contacts = self._contacts_payload(
            config.get("contact_entity_ids") or []
            if contacts_picked
            else self._auto_contact_ids(scope),
            scope,
        )
        suggestions = getattr(loaded_runtime_data(self._hass), "suggestions", None)
        contextual = (
            await suggestions.payload(member_id, scope)
            if suggestions
            else {
                "version": 1,
                "status": "unavailable",
                "suggestion": None,
            }
        )
        # suggested_routine is a scene an admin featured by hand, shown only
        # while no contextual rules exist.
        legacy = (
            visible_scene(config.get("suggested_scene_id"))
            if suggestions is None or contextual["status"] == "not_configured"
            else None
        )
        return self.json(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "recently_used": {"source": recent_source, "items": recent_items},
                "pinned_moments": [
                    scene
                    for scene_id in config.get("pinned_scene_ids", [])
                    if (scene := visible_scene(scene_id)) is not None
                ],
                "suggested_routine": legacy,
                "suggested_routine_source": "featured_manual" if legacy else None,
                "contextual_suggestion": contextual,
                "room_activity": {
                    "rooms": active_rooms,
                    "layout": room_activity_layout(active_rooms),
                    # Rooms an admin's stored policy makes take part (rooms
                    # without one take part too, but aren't configured).
                    "configured_room_count": sum(
                        1
                        for room_id in room_ids
                        if policies.get(room_id, {}).get("participates", False)
                        and not policies[room_id]["default"]
                    ),
                    "ranking": ranking_summary(
                        active_rooms,
                        habits.summary
                        if habits is not None
                        else {"layer": layer, "status": STATUS_UNAVAILABLE},
                    ),
                },
                "outdoor_weather": weather,
                "air_quality": air_quality,
                "doors_windows": contacts,
            }
        )

    def _recent_entry(
        self, entity_id: str, at: str | None, claims: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A recently used device for the page, or None if the caller can't see it.

        A favorite can be a camera, so this takes the camera access into
        account (filtering.visible_to), not only the room scope.
        """
        state = self._hass.states.get(entity_id)
        if (
            state is None
            or not is_served(self._hass, entity_id)
            or not visible_to(self._hass, entity_id, claims)
        ):
            return None
        return {
            "entity_id": entity_id,
            "at": at,
            "device": serialize_device(self._hass, state),
        }

    def _weather_payload(self, entity_id: str | None) -> dict[str, Any]:
        """The configured weather entity's reading, or why there is none."""
        if not entity_id:
            return {"available": False, "reason": "not_configured"}
        state = self._hass.states.get(entity_id)
        if _state_is_unavailable(state):
            return {"available": True, "status": "unavailable", "entity_id": entity_id}
        return {
            "available": True,
            "status": "stale" if _state_stale(state) else "available",
            "entity_id": entity_id,
            "temperature": state.attributes.get("temperature"),
            "temperature_unit": state.attributes.get("temperature_unit"),
            "humidity": state.attributes.get("humidity"),
        }

    def _air_quality_payload(self, entity_id: str | None) -> dict[str, Any]:
        """The configured air-quality sensor's reading, or why there is none."""
        if not entity_id:
            return {"supported": False}
        state = self._hass.states.get(entity_id)
        if _state_is_unavailable(state):
            return {"supported": True, "status": "unavailable", "entity_id": entity_id}
        return {
            "supported": True,
            "status": "stale" if _state_stale(state) else "available",
            "entity_id": entity_id,
            "value": state.state,
            "unit": state.attributes.get("unit_of_measurement"),
        }

    def _auto_contact_ids(self, scope: list[str] | None) -> list[str]:
        """Every door/window/opening sensor the caller can see, sorted."""
        return sorted(
            state.entity_id
            for state in self._hass.states.async_all("binary_sensor")
            if self._device_class(state.entity_id, state) in _CONTACT_DEVICE_CLASSES
            and is_served(self._hass, state.entity_id)
            and in_scope(self._hass, state.entity_id, scope)
        )

    def _contacts_payload(
        self, entity_ids: list[str], scope: list[str] | None
    ) -> dict[str, Any]:
        """Open/closed summary of the configured doors, windows and locks.

        A contact outside the caller's scope counts as unknown.
        """
        if not entity_ids:
            return {"available": False}
        contact_states: list[tuple[str, str | None]] = []
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            contact_states.append(
                (
                    entity_id,
                    state.state
                    if state is not None and in_scope(self._hass, entity_id, scope)
                    else None,
                )
            )
        aggregate = summarize_openings(contact_states)
        return {
            "available": True,
            "status": aggregate["status"],
            "entity_ids": list(entity_ids),
            "configured_count": len(entity_ids),
            "open_count": aggregate["open_count"],
            "unknown_count": aggregate["unknown_count"],
        }


class CasaSmartNowConfigView(_NowView):
    """GET/PUT /api/casasmart/now/config."""

    url = f"/api/{DOMAIN}/now/config"
    name = f"api:{DOMAIN}:now:config"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        return self.json(await self._hass.async_add_executor_job(now_data.config))

    async def put(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        error_response = await self._validate_configuration(payload)
        if error_response is not None:
            return error_response
        try:
            config = await self._hass.async_add_executor_job(
                now_data.configure, payload
            )
        except NowDataError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(config)

    async def _validate_configuration(
        self, payload: dict[str, Any]
    ) -> web.Response | None:
        """A 400 when a configured entity or scene is missing or the wrong kind."""
        weather_id = payload.get("outdoor_weather_entity_id")
        if weather_id is not None:
            if not isinstance(weather_id, str) or not self._is_openweathermap_weather(
                weather_id
            ):
                return self.json_message(
                    "outdoor_weather_entity_id must be a configured OpenWeatherMap weather entity",
                    HTTPStatus.BAD_REQUEST,
                )
        aq_id = payload.get("air_quality_entity_id")
        if aq_id is not None and not self._is_air_quality_sensor(aq_id):
            return self.json_message(
                "air_quality_entity_id must be a compatible air-quality sensor",
                HTTPStatus.BAD_REQUEST,
            )
        contacts = payload.get("contact_entity_ids")
        if contacts is not None and isinstance(contacts, list):
            for entity_id in contacts:
                if not self._is_contact_sensor(entity_id):
                    return self.json_message(
                        f"Configured contact {entity_id!r} is not a door/window contact",
                        HTTPStatus.BAD_REQUEST,
                    )
        registry = self._registry()
        if registry is not None:
            known_scenes = {
                scene["scene_id"]
                for scene in await self._hass.async_add_executor_job(
                    registry.list_scenes
                )
            }
            # Only existence is checked here; NowDataEngine.configure rejects
            # values of the wrong type with its own 400.
            pinned = payload.get("pinned_scene_ids")
            for scene_id in [
                payload.get("suggested_scene_id"),
                *(pinned if isinstance(pinned, list) else []),
            ]:
                if isinstance(scene_id, str) and scene_id not in known_scenes:
                    return self.json_message(
                        f"Configured scene {scene_id!r} not found",
                        HTTPStatus.BAD_REQUEST,
                    )
        return None

    def _is_openweathermap_weather(self, entity_id: str) -> bool:
        """A weather entity that belongs to an OpenWeatherMap config entry."""
        if (
            not entity_id.startswith("weather.")
            or self._hass.states.get(entity_id) is None
        ):
            return False
        entry = er.async_get(self._hass).async_get(entity_id)
        if entry is None or not entry.config_entry_id:
            return False
        config_entry = self._hass.config_entries.async_get_entry(entry.config_entry_id)
        return config_entry is not None and config_entry.domain == "openweathermap"

    def _is_air_quality_sensor(self, entity_id: object) -> bool:
        """A sensor whose device class is an air-quality measurement."""
        if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
            return False
        state = self._hass.states.get(entity_id)
        if state is None:
            return False
        return self._device_class(entity_id, state) in _AIR_QUALITY_DEVICE_CLASSES

    def _is_contact_sensor(self, entity_id: object) -> bool:
        """A lock, or a door/window/opening binary sensor."""
        if not isinstance(entity_id, str):
            return False
        state = self._hass.states.get(entity_id)
        if state is None:
            return False
        if entity_id.startswith("lock."):
            return True
        if not entity_id.startswith("binary_sensor."):
            return False
        return self._device_class(entity_id, state) in _CONTACT_DEVICE_CLASSES


class CasaSmartNowHabitsView(_NowView):
    """GET/PUT /api/casasmart/now/habits: room habit learning (admin only).

    GET answers the settings and how far learning is: {learning, paused,
    status, weekend_days, self_scheduled_entity_ids, reset_at,
    observed_days, warm_up_days, profile, ramadan}; profile is today's
    (regular or ramadan), ramadan the current or next Ramadan with the
    years kept for it. PUT changes any of learning, paused,
    weekend_days (Monday = 0) and self_scheduled_entity_ids (devices the hub
    manages whose wall-switch events are a schedule kept on the device), and
    answers like GET.
    """

    url = f"/api/{DOMAIN}/now/habits"
    name = f"api:{DOMAIN}:now:habits"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        return await self._answer()

    async def put(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        learner, registry = self._learner(), self._registry()
        if learner is None or registry is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        try:
            _, managed = await async_counted_controls(self._hass, registry)
            await learner.async_update(payload, managed_ids=managed)
        except HabitSettingsError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        return await self._answer()

    def _learner(self) -> Any:
        return getattr(loaded_runtime_data(self._hass), "room_habits", None)

    async def _answer(self) -> web.Response:
        """The settings and learning status, over every room."""
        learner, registry = self._learner(), self._registry()
        if learner is None or registry is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            counted, _ = await async_counted_controls(self._hass, registry)
            rooms = await self._hass.async_add_executor_job(registry.list_rooms)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        return self.json(
            await learner.async_settings([room["room_id"] for room in rooms], counted)
        )


class CasaSmartNowHabitsResetView(CasaSmartNowHabitsView):
    """POST /api/casasmart/now/habits/reset: forget every learned habit.

    The kept Ramadans go too. Learning starts over the next day (warm-up
    included); the activity log itself is kept. Answers like GET /now/habits.
    """

    url = f"/api/{DOMAIN}/now/habits/reset"
    name = f"api:{DOMAIN}:now:habits:reset"
    # Inherited for the answer only: settings are read and changed on
    # /now/habits, so this path answers GET and PUT with 405.
    get = None
    put = None

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        learner = self._learner()
        if learner is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            await learner.async_reset()
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        return await self._answer()


class CasaSmartRoomActivityPolicyView(_NowView):
    """PUT /api/casasmart/now/rooms/{room_id}/activity-policy."""

    url = f"/api/{DOMAIN}/now/rooms/{{room_id}}/activity-policy"
    name = f"api:{DOMAIN}:now:room:activity-policy"

    async def put(self, request: web.Request, room_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        if not await self._room_accessible(room_id, claims.get("rooms")):
            return self.json_message("Unknown room", HTTPStatus.NOT_FOUND)
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        eligible_entity_ids = payload.get("eligible_entity_ids")
        if not isinstance(eligible_entity_ids, list):
            return self.json_message(
                "eligible_entity_ids must be an explicit list of approved entities",
                HTTPStatus.BAD_REQUEST,
            )
        invalid = await self._invalid_eligible_entities(room_id, eligible_entity_ids)
        if invalid is not None:
            return self.json_message(invalid, HTTPStatus.BAD_REQUEST)
        try:
            policy = await self._hass.async_add_executor_job(
                now_data.set_room_policy,
                room_id,
                payload.get("participates"),
                eligible_entity_ids,
            )
        except NowDataError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(policy)

    async def _invalid_eligible_entities(
        self, room_id: str, entity_ids: list[Any]
    ) -> str | None:
        """Why the list cannot be the room's policy, or None if it can.

        Every entity must be served, in this room, and a safe candidate (a
        wall relay typed as a light or fan is one).
        """
        if len(entity_ids) > 64 or any(
            not isinstance(entity_id, str) for entity_id in entity_ids
        ):
            return "eligible_entity_ids must contain at most 64 entity ids"
        if len(set(entity_ids)) != len(entity_ids):
            return "eligible_entity_ids must not contain duplicates"
        gang_types = await self._gang_types()
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            if (
                state is None
                or area_id_of(self._hass, entity_id) != room_id
                or not is_served(self._hass, entity_id)
                or not is_room_activity_candidate(state, gang_types.get(entity_id))
            ):
                return f"Entity {entity_id!r} is not an eligible room-activity device"
        return None

    async def _gang_types(self) -> dict[str, str | None]:
        """The registry's imported controls with their gang types ({} while
        the hub isn't loaded)."""
        registry = self._registry()
        if registry is None:
            return {}
        return imported_controls(
            await self._hass.async_add_executor_job(registry.list_user_devices)
        )

    async def _room_accessible(self, room_id: str, scope: list[str] | None) -> bool:
        """True for a registry room inside the caller's scope."""
        registry = self._registry()
        if registry is None or (scope is not None and room_id not in scope):
            return False
        rooms = await self._hass.async_add_executor_job(registry.list_rooms)
        return any(room["room_id"] == room_id for room in rooms)


class CasaSmartRoomStartProfileView(CasaSmartRoomActivityPolicyView):
    """GET/PUT/DELETE /api/casasmart/now/rooms/{room_id}/start-profile.

    The lights and fans one tap "on" turns on in a room that is off when the
    hub knows nothing better (no recent capture, no last-on set). An
    installer's choice, so registry.manage, like the room's policy. PUT
    {entity_ids: [...]} sets it (1 to 64 of the room's lights and fans);
    DELETE clears it. Each answers {room_id, entity_ids}. Only the devices
    the room's policy approves ever come on.
    """

    url = f"/api/{DOMAIN}/now/rooms/{{room_id}}/start-profile"
    name = f"api:{DOMAIN}:now:room:start-profile"

    async def _checked(
        self, request: web.Request, room_id: str
    ) -> tuple[NowDataEngine | None, web.Response | None]:
        """The Now store, or the refusal (auth, unknown room, not ready)."""
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return None, error
        if not await self._room_accessible(room_id, claims.get("rooms")):
            return None, self.json_message("Unknown room", HTTPStatus.NOT_FOUND)
        return self._now_or_503()

    async def get(self, request: web.Request, room_id: str) -> web.Response:
        now_data, error = await self._checked(request, room_id)
        if error is not None:
            return error
        entity_ids = await self._hass.async_add_executor_job(
            now_data.start_profile, room_id
        )
        return self.json({"room_id": room_id, "entity_ids": entity_ids})

    async def put(self, request: web.Request, room_id: str) -> web.Response:
        now_data, error = await self._checked(request, room_id)
        if error is not None:
            return error
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        unknown = sorted(set(payload) - {"entity_ids"})
        if unknown:
            return self.json_message(
                f"Unknown start profile field: {unknown[0]!r}", HTTPStatus.BAD_REQUEST
            )
        entity_ids = payload.get("entity_ids")
        if not isinstance(entity_ids, list) or not entity_ids:
            return self.json_message(
                "entity_ids must list the room's lights and fans "
                "(DELETE clears the start profile)",
                HTTPStatus.BAD_REQUEST,
            )
        invalid = await self._invalid_start_profile(room_id, entity_ids)
        if invalid is not None:
            return self.json_message(invalid, HTTPStatus.BAD_REQUEST)
        try:
            profile = await self._hass.async_add_executor_job(
                now_data.set_start_profile, room_id, entity_ids
            )
        except NowDataError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(profile)

    async def delete(self, request: web.Request, room_id: str) -> web.Response:
        now_data, error = await self._checked(request, room_id)
        if error is not None:
            return error
        return self.json(
            await self._hass.async_add_executor_job(
                now_data.clear_start_profile, room_id
            )
        )

    async def _invalid_start_profile(
        self, room_id: str, entity_ids: list[Any]
    ) -> str | None:
        """Why the list cannot be the room's start profile, or None if it can.

        Every entity must be a light or fan in this room (a wall relay the
        installer typed as one too: now_data.lighting_kind), served, and not
        left out of room activity.
        """
        if len(entity_ids) > MAX_START_PROFILE or any(
            not isinstance(entity_id, str) for entity_id in entity_ids
        ):
            return f"entity_ids must contain at most {MAX_START_PROFILE} entity ids"
        if len(set(entity_ids)) != len(entity_ids):
            return "entity_ids must not contain duplicates"
        gang_types = await self._gang_types()
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            gang_type = gang_types.get(entity_id)
            if not is_on_domain(entity_id) and (
                not entity_id.startswith(f"{RELAY_DOMAIN}.")
                or state is None
                or lighting_kind(state, gang_type) is None
            ):
                return (
                    f"Entity {entity_id!r} is not a light or fan: one tap only "
                    "turns on lights and fans (a switch only when its installer "
                    "typed it as a light or fan)"
                )
            if (
                state is None
                or area_id_of(self._hass, entity_id) != room_id
                or not is_served(self._hass, entity_id)
                or lighting_kind(state, gang_type) is None
            ):
                return f"Entity {entity_id!r} is not a light or fan in this room"
        return None


class CasaSmartRoomActivityCommandView(CasaSmartRoomActivityPolicyView):
    """POST /api/casasmart/now/rooms/{room_id}/activity.

    The body is {action: turn_off|turn_on, idempotency_key}. Commands run one
    at a time per room, and a retry with the same key replays the answer. An
    on turns on only lights and fans, from the room's plan
    (room_intel.one_tap); with nothing to turn on it is refused with 409
    nothing_to_turn_on and open_room: true, before anything is switched.
    """

    url = f"/api/{DOMAIN}/now/rooms/{{room_id}}/activity"
    name = f"api:{DOMAIN}:now:room:activity"
    # Inherited for the room checks only: the policy is set on
    # /activity-policy, so this path answers PUT with 405.
    put = None

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        # Shared across view instances and listeners; see _room_command_locks.
        self._room_locks = _room_command_locks(hass)

    async def post(self, request: web.Request, room_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.control")
        if error is not None:
            return error
        if not await self._room_accessible(room_id, claims.get("rooms")):
            return self.json_message("Unknown room", HTTPStatus.NOT_FOUND)
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        runtime_data = loaded_runtime_data(self._hass)
        if (
            runtime_data is not None
            and runtime_data.energy is not None
            and energy_lockout_applies(runtime_data.energy, claims)
        ):
            return self.json(energy_lockout_refusal(), HTTPStatus.FORBIDDEN)
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        action = payload.get("action")
        if action not in {"turn_off", "turn_on"}:
            return self.json_message(
                "action must be turn_off or turn_on", HTTPStatus.BAD_REQUEST
            )
        try:
            key = now_data.validate_idempotency_key(payload.get("idempotency_key"))
        except NowDataError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        try:
            member_id = await async_member_id(self._hass, claims)
        except (StorageError, sqlite3.Error) as err:
            return self._storage_failure(err)
        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        # The replay check, the command and storing its answer happen under
        # the room's lock, so a retry waits for the original and replays it.
        async with lock:
            existing = await self._hass.async_add_executor_job(
                now_data.idempotent_result, member_id, room_id, action, key
            )
            if existing is not None:
                return self.json(existing)
            participates = await self._hass.async_add_executor_job(
                now_data.room_participates, room_id
            )
            if not participates:
                return self.json(
                    {
                        "error": "room_activity_disabled",
                        "message": "Room activity is not enabled for this room",
                    },
                    HTTPStatus.CONFLICT,
                )
            result = await self._run(
                room_id,
                action,
                now_data,
                command_context(self._hass, SOURCE_APP, member_id),
            )
            if isinstance(result, RoomOnPlan):
                # Nothing was switched, so there is no answer to replay. The
                # app opens the room instead of saying it couldn't turn on.
                return self.json(
                    {
                        "error": "nothing_to_turn_on",
                        "reason": result.reason,
                        "message": _NOTHING_TO_TURN_ON[result.reason],
                        "open_room": True,
                    },
                    HTTPStatus.CONFLICT,
                )
            result["idempotency_key"] = key
            await self._hass.async_add_executor_job(
                now_data.save_idempotent_result, member_id, room_id, action, key, result
            )
            for outcome in result["outcomes"]:
                if outcome["outcome"] == "changed":
                    await self._hass.async_add_executor_job(
                        now_data.record_successful_control,
                        member_id,
                        outcome["entity_id"],
                    )
            return self.json(result)

    async def _run(
        self,
        room_id: str,
        action: str,
        now_data: NowDataEngine,
        context: Context | None = None,
    ) -> dict[str, Any] | RoomOnPlan:
        """Switch the room's eligible devices and report each one's outcome.

        Off targets every eligible device that is on. On targets the room's
        plan (room_intel.one_tap.plan_room: the hub's capture, the last-on
        set or the start profile; lights and fans only, wall relays typed as
        one included) that is still off;
        the plan's unavailable devices are reported, not switched. A device
        counts as changed only when its state afterwards confirms it. An on
        with nothing to turn on returns that plan (no source), before
        anything is switched. The calls carry context (the member's, for the
        room activity log).
        """
        policy = await self._hass.async_add_executor_job(now_data.room_policy, room_id)
        # Which wall relays the installer typed as a light or fan, and (in a
        # room without a stored policy) which devices are imported.
        gang_types = await self._gang_types()
        eligible = {
            state.entity_id: state
            for state in self._hass.states.async_all()
            if area_id_of(self._hass, state.entity_id) == room_id
            and is_served(self._hass, state.entity_id)
            and _approved(state, policy, gang_types)
        }
        plan: RoomOnPlan | None = None
        if action == "turn_off":
            target_ids = [
                entity_id
                for entity_id, state in eligible.items()
                if state.state == "on"
            ]
        else:
            # Lights and fans only: a switch only when it is a wall relay
            # the installer typed as a light or fan.
            kinds = {
                entity_id: kind
                for entity_id, state in eligible.items()
                if (kind := lighting_kind(state, gang_types.get(entity_id))) is not None
            }
            devices = {entity_id: eligible[entity_id].state for entity_id in kinds}

            def read_sources() -> tuple:
                return now_data.restore_capture(room_id), now_data.start_profile(
                    room_id
                )

            capture, start_profile = await self._hass.async_add_executor_job(
                read_sources
            )
            last_on = _last_on(self._hass, room_id)
            now_t = time.time()
            plan = plan_room(
                capture=capture,
                last_on=last_on,
                start_profile=start_profile,
                devices=devices,
                now_t=now_t,
                kinds=kinds,
            )
            capture_ids, captured_at = capture
            if (
                capture_ids
                and plan.source != SOURCE_CAPTURE
                and (
                    not capture_counts(captured_at, last_on, now_t)
                    or not present_ids(capture_ids, devices, kinds)
                )
            ):
                # Too old, outdated by a later dark room, or nothing in it
                # is still one of the room's lights and fans: dropped, so it
                # never comes back as the room's restore.
                await self._hass.async_add_executor_job(
                    now_data.consume_restore_set, room_id
                )
            if plan.source is None:
                return plan
            target_ids = [
                entity_id for entity_id in plan.targets if devices[entity_id] != "on"
            ]
        outcomes = [
            {"entity_id": entity_id, "outcome": "pending"} for entity_id in target_ids
        ]
        by_domain: dict[str, list[str]] = {}
        for entity_id in target_ids:
            by_domain.setdefault(entity_id.split(".", 1)[0], []).append(entity_id)
        failures: set[str] = set()
        for domain, entity_ids in by_domain.items():
            try:
                await self._hass.services.async_call(
                    domain,
                    action,
                    {"entity_id": entity_ids},
                    blocking=True,
                    context=context,
                )
            except HomeAssistantError:
                failures.update(entity_ids)
        changed_ids: list[str] = []
        expected = "off" if action == "turn_off" else "on"
        for outcome in outcomes:
            entity_id = outcome["entity_id"]
            current = self._hass.states.get(entity_id)
            if (
                entity_id not in failures
                and current is not None
                and current.state == expected
            ):
                outcome["outcome"] = "changed"
                changed_ids.append(entity_id)
            else:
                outcome["outcome"] = "failed"
        failed_ids = [
            item["entity_id"] for item in outcomes if item["outcome"] == "failed"
        ]
        if action == "turn_off":
            # Add to the capture rather than replace it: a repeated off finds
            # little still on and must not forget what the first one captured.
            restore_pending = await self._hass.async_add_executor_job(
                now_data.extend_restore_set, room_id, changed_ids
            )
            return {
                "ok": not failed_ids,
                "room_id": room_id,
                "action": action,
                "outcomes": outcomes,
                "restore_pending_count": len(restore_pending),
                "restored_from_capture_count": 0,
            }
        # Offline devices are reported, never sent a command.
        outcomes.extend(
            {"entity_id": entity_id, "outcome": "unavailable"}
            for entity_id in plan.unavailable
        )
        not_on = [*failed_ids, *plan.unavailable]
        restore_pending = not_on
        if plan.source == SOURCE_CAPTURE:
            if not_on:
                # What didn't come on stays captured, so the next on retries
                # just those; the rest of the capture is done.
                restore_pending = await self._hass.async_add_executor_job(
                    now_data.keep_in_restore_set, room_id, not_on
                )
            else:
                await self._hass.async_add_executor_job(
                    now_data.consume_restore_set, room_id
                )
        # The plan's devices that are on now: switched on here, or already on
        # (not the ones that failed, are offline or left the room).
        restored = len(plan.targets) - len(failed_ids)
        return {
            "ok": not not_on,
            "room_id": room_id,
            "action": action,
            "outcomes": outcomes,
            "restore_pending_count": len(restore_pending),
            "restored_from_capture_count": restored
            if plan.source == SOURCE_CAPTURE
            else 0,
            "source": plan.source,
            "restored_count": restored,
        }
