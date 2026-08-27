"""Authenticated CasaSmart Now snapshot and room-activity endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .auth_api import authenticate_request, get_engine, json_body
from .const import DOMAIN
from .energy_runtime import energy_lockout_applies
from .filtering import area_id_of, in_scope, is_served, serialize_device
from .now_data import (
    NowDataEngine,
    NowDataError,
    is_room_activity_candidate,
    is_room_activity_eligible,
    is_running_room_activity,
    room_activity_layout,
)
from .registry import RegistryEngine

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData


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


def get_now_data(hass: HomeAssistant) -> NowDataEngine | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.now_data


def _runtime_data(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _member_id(hass: HomeAssistant, claims: dict[str, Any]) -> str:
    engine = get_engine(hass)
    return engine.member_id_for(claims["sub"]) if engine else claims["sub"]


def _state_is_unavailable(state: Any) -> bool:
    return state is None or state.state in {"unknown", "unavailable"}


def _state_stale(state: Any) -> bool:
    if _state_is_unavailable(state):
        return False
    changed = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    if not isinstance(changed, datetime):
        return False
    return (datetime.now(timezone.utc) - changed.astimezone(timezone.utc)).total_seconds() > _STALE_SECONDS


class _NowView(HomeAssistantView):
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _now_or_503(self) -> tuple[NowDataEngine | None, web.Response | None]:
        now_data = get_now_data(self._hass)
        if now_data is None:
            return None, self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        return now_data, None

    def _registry(self) -> RegistryEngine | None:
        data = _runtime_data(self._hass)
        return data.registry if data is not None else None


class CasaSmartNowView(_NowView):
    """Return the server-computed, user-scoped Now snapshot."""

    url = f"/api/{DOMAIN}/now"
    name = f"api:{DOMAIN}:now"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        registry = self._registry()
        if registry is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        member_id = _member_id(self._hass, claims)
        scope = claims.get("rooms")
        recents, config, rooms, scenes, favorites, policies = await self._hass.async_add_executor_job(
            lambda: (
                now_data.recents_for(member_id),
                now_data.config(),
                registry.list_rooms(),
                registry.list_scenes(),
                registry.get_favorites(member_id),
                {room["room_id"]: now_data.room_policy(room["room_id"]) for room in registry.list_rooms()},
            )
        )
        rooms = [room for room in rooms if scope is None or room["room_id"] in scope]
        room_ids = {room["room_id"] for room in rooms}

        recent_items = []
        for item in recents:
            entity_id = item["entity_id"]
            state = self._hass.states.get(entity_id)
            if state is None or not is_served(self._hass, entity_id) or not in_scope(self._hass, entity_id, scope):
                continue
            recent_items.append({"entity_id": entity_id, "at": item["at"], "device": serialize_device(self._hass, state)})
        recent_source = "recency"
        if not recent_items:
            recent_source = "favorites_seed"
            for entity_id in favorites:
                state = self._hass.states.get(entity_id)
                if state is None or not is_served(self._hass, entity_id) or not in_scope(self._hass, entity_id, scope):
                    continue
                recent_items.append({"entity_id": entity_id, "at": None, "device": serialize_device(self._hass, state)})

        active_rooms: list[dict[str, Any]] = []
        for room in rooms:
            room_id = room["room_id"]
            policy = policies.get(room_id, {})
            if not policy.get("participates", False):
                continue
            eligible_entity_ids = policy.get("eligible_entity_ids", [])
            devices = [
                state
                for state in self._hass.states.async_all()
                if area_id_of(self._hass, state.entity_id) == room_id
                and is_served(self._hass, state.entity_id)
                and in_scope(self._hass, state.entity_id, scope)
                and is_running_room_activity(state, eligible_entity_ids)
            ]
            timestamps = [
                state.last_changed for state in devices if isinstance(state.last_changed, datetime)
            ]
            restore_pending_count = len(
                await self._hass.async_add_executor_job(now_data.restore_set, room_id)
            )
            active_rooms.append(
                {
                    "room_id": room_id,
                    "name": room["name"],
                    "icon": room.get("icon"),
                    # The tablet derives the visible switch/count from its
                    # live state feed so a successful command repaints in the
                    # same frame. Exposing the already-validated policy list
                    # lets it preserve both the Hub policy and its own strict
                    # actuator allowlist instead of guessing membership.
                    "eligible_entity_ids": list(eligible_entity_ids),
                    "active_count": len(devices),
                    "most_recent_activity_at": max(timestamps).astimezone(timezone.utc).isoformat() if timestamps else None,
                    "restore_pending_count": restore_pending_count,
                }
            )
        active_rooms.sort(
            key=lambda room: (
                -room["active_count"],
                -(datetime.fromisoformat(room["most_recent_activity_at"]).timestamp() if room["most_recent_activity_at"] else 0),
                room["name"].strip().casefold(),
                room["room_id"],
            )
        )

        scenes_by_id = {scene["scene_id"]: scene for scene in scenes}
        def visible_scene(scene_id: str | None) -> dict[str, Any] | None:
            scene = scenes_by_id.get(scene_id) if scene_id else None
            if scene is None or not all(in_scope(self._hass, item["entity_id"], scope) for item in scene["entities"]):
                return None
            return {key: scene[key] for key in ("scene_id", "name", "icon")}

        weather = self._weather_payload(config.get("outdoor_weather_entity_id"))
        air_quality = self._air_quality_payload(config.get("air_quality_entity_id"))
        contacts = self._contacts_payload(config.get("contact_entity_ids") or [], scope)
        return self.json(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "recently_used": {"source": recent_source, "items": recent_items},
                "pinned_moments": [scene for scene_id in config.get("pinned_scene_ids", []) if (scene := visible_scene(scene_id)) is not None],
                "suggested_routine": visible_scene(config.get("suggested_scene_id")),
                "room_activity": {"rooms": active_rooms, "layout": room_activity_layout(active_rooms), "configured_room_count": sum(1 for room_id in room_ids if policies.get(room_id, {}).get("participates", False))},
                "outdoor_weather": weather,
                "air_quality": air_quality,
                "doors_windows": contacts,
            }
        )

    def _weather_payload(self, entity_id: str | None) -> dict[str, Any]:
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

    def _contacts_payload(self, entity_ids: list[str], scope: list[str] | None) -> dict[str, Any]:
        if not entity_ids:
            return {"available": False}
        opened = 0
        unknown = 0
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            if state is None or not in_scope(self._hass, entity_id, scope) or state.state in {"unknown", "unavailable"}:
                unknown += 1
            elif state.state == "on":
                opened += 1
        status = "open" if opened else ("unknown" if unknown else "all_closed")
        return {"available": True, "status": status, "configured_count": len(entity_ids), "open_count": opened, "unknown_count": unknown}


class CasaSmartNowConfigView(_NowView):
    """Store explicit Hub configuration; this never auto-selects sensors."""

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
        payload = await json_body(request)
        if payload is None:
            return self.json_message("Body must be a JSON object", HTTPStatus.BAD_REQUEST)
        error_response = await self._validate_configuration(payload)
        if error_response is not None:
            return error_response
        try:
            config = await self._hass.async_add_executor_job(now_data.configure, payload)
        except NowDataError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(config)

    async def _validate_configuration(self, payload: dict[str, Any]) -> web.Response | None:
        weather_id = payload.get("outdoor_weather_entity_id")
        if weather_id is not None:
            if not isinstance(weather_id, str) or not self._is_openweathermap_weather(weather_id):
                return self.json_message("outdoor_weather_entity_id must be a configured OpenWeatherMap weather entity", HTTPStatus.BAD_REQUEST)
        aq_id = payload.get("air_quality_entity_id")
        if aq_id is not None and not self._is_air_quality_sensor(aq_id):
            return self.json_message("air_quality_entity_id must be a compatible air-quality sensor", HTTPStatus.BAD_REQUEST)
        contacts = payload.get("contact_entity_ids")
        if contacts is not None and isinstance(contacts, list):
            for entity_id in contacts:
                if not self._is_contact_sensor(entity_id):
                    return self.json_message(f"Configured contact {entity_id!r} is not a door/window contact", HTTPStatus.BAD_REQUEST)
        registry = self._registry()
        if registry is not None:
            known_scenes = {
                scene["scene_id"]
                for scene in await self._hass.async_add_executor_job(registry.list_scenes)
            }
            for scene_id in [payload.get("suggested_scene_id"), *(payload.get("pinned_scene_ids") or [])]:
                if scene_id is not None and scene_id not in known_scenes:
                    return self.json_message(f"Configured scene {scene_id!r} not found", HTTPStatus.BAD_REQUEST)
        return None

    def _is_openweathermap_weather(self, entity_id: str) -> bool:
        if not entity_id.startswith("weather.") or self._hass.states.get(entity_id) is None:
            return False
        entry = er.async_get(self._hass).async_get(entity_id)
        if entry is None or not entry.config_entry_id:
            return False
        config_entry = self._hass.config_entries.async_get_entry(entry.config_entry_id)
        return config_entry is not None and config_entry.domain == "openweathermap"

    def _is_air_quality_sensor(self, entity_id: object) -> bool:
        if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
            return False
        state = self._hass.states.get(entity_id)
        if state is None:
            return False
        entry = er.async_get(self._hass).async_get(entity_id)
        device_class = state.attributes.get("device_class")
        if entry is not None and entry.original_device_class:
            device_class = entry.original_device_class
        return device_class in _AIR_QUALITY_DEVICE_CLASSES

    def _is_contact_sensor(self, entity_id: object) -> bool:
        if not isinstance(entity_id, str) or not entity_id.startswith("binary_sensor."):
            return False
        state = self._hass.states.get(entity_id)
        if state is None:
            return False
        entry = er.async_get(self._hass).async_get(entity_id)
        device_class = state.attributes.get("device_class")
        if entry is not None and entry.original_device_class:
            device_class = entry.original_device_class
        return device_class in _CONTACT_DEVICE_CLASSES


class CasaSmartRoomActivityPolicyView(_NowView):
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
        payload = await json_body(request)
        if payload is None:
            return self.json_message("Body must be a JSON object", HTTPStatus.BAD_REQUEST)
        eligible_entity_ids = payload.get("eligible_entity_ids")
        if not isinstance(eligible_entity_ids, list):
            return self.json_message("eligible_entity_ids must be an explicit list of approved entities", HTTPStatus.BAD_REQUEST)
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
        if len(entity_ids) > 64 or any(not isinstance(entity_id, str) for entity_id in entity_ids):
            return "eligible_entity_ids must contain at most 64 entity ids"
        if len(set(entity_ids)) != len(entity_ids):
            return "eligible_entity_ids must not contain duplicates"
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            if (
                state is None
                or area_id_of(self._hass, entity_id) != room_id
                or not is_served(self._hass, entity_id)
                or not is_room_activity_candidate(state)
            ):
                return f"Entity {entity_id!r} is not an eligible room-activity device"
        return None

    async def _room_accessible(self, room_id: str, scope: list[str] | None) -> bool:
        registry = self._registry()
        if registry is None or (scope is not None and room_id not in scope):
            return False
        rooms = await self._hass.async_add_executor_job(registry.list_rooms)
        return any(room["room_id"] == room_id for room in rooms)


class CasaSmartRoomActivityCommandView(CasaSmartRoomActivityPolicyView):
    """One lock-protected Hub command with durable idempotency/restore state."""

    url = f"/api/{DOMAIN}/now/rooms/{{room_id}}/activity"
    name = f"api:{DOMAIN}:now:room:activity"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self._room_locks: dict[str, asyncio.Lock] = {}

    async def post(self, request: web.Request, room_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.control")
        if error is not None:
            return error
        if not await self._room_accessible(room_id, claims.get("rooms")):
            return self.json_message("Unknown room", HTTPStatus.NOT_FOUND)
        now_data, not_ready = self._now_or_503()
        if not_ready is not None:
            return not_ready
        runtime_data = _runtime_data(self._hass)
        if runtime_data is not None and runtime_data.energy is not None and energy_lockout_applies(runtime_data.energy, claims):
            return self.json({"error": "energy_lockout", "message": "Energy saving is active — controls are locked by the admin"}, HTTPStatus.FORBIDDEN)
        payload = await json_body(request)
        if payload is None:
            return self.json_message("Body must be a JSON object", HTTPStatus.BAD_REQUEST)
        action = payload.get("action")
        if action not in {"turn_off", "turn_on"}:
            return self.json_message("action must be turn_off or turn_on", HTTPStatus.BAD_REQUEST)
        try:
            key = now_data.validate_idempotency_key(payload.get("idempotency_key"))
        except NowDataError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        member_id = _member_id(self._hass, claims)
        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            existing = await self._hass.async_add_executor_job(
                now_data.idempotent_result, member_id, room_id, action, key
            )
            if existing is not None:
                return self.json(existing)
            participates = await self._hass.async_add_executor_job(now_data.room_participates, room_id)
            if not participates:
                return self.json({"error": "room_activity_disabled", "message": "Room activity is not enabled for this room"}, HTTPStatus.CONFLICT)
            result = await self._run(room_id, action, now_data)
            result["idempotency_key"] = key
            await self._hass.async_add_executor_job(
                now_data.save_idempotent_result, member_id, room_id, action, key, result
            )
            for outcome in result["outcomes"]:
                if outcome["outcome"] == "changed":
                    await self._hass.async_add_executor_job(now_data.record_successful_control, member_id, outcome["entity_id"])
            return self.json(result)

    async def _run(self, room_id: str, action: str, now_data: NowDataEngine) -> dict[str, Any]:
        policy = await self._hass.async_add_executor_job(now_data.room_policy, room_id)
        eligible_entity_ids = policy["eligible_entity_ids"]
        eligible = {
            state.entity_id: state
            for state in self._hass.states.async_all()
            if area_id_of(self._hass, state.entity_id) == room_id
            and is_served(self._hass, state.entity_id)
            and is_room_activity_eligible(state, eligible_entity_ids)
        }
        restore_ids: list[str] = []
        if action == "turn_off":
            target_ids = [entity_id for entity_id, state in eligible.items() if state.state == "on"]
        else:
            restore_ids = await self._hass.async_add_executor_job(now_data.restore_set, room_id)
            target_ids = [entity_id for entity_id in restore_ids if entity_id in eligible and eligible[entity_id].state != "on"]
        outcomes = [{"entity_id": entity_id, "outcome": "pending"} for entity_id in target_ids]
        by_domain: dict[str, list[str]] = {}
        for entity_id in target_ids:
            by_domain.setdefault(entity_id.split(".", 1)[0], []).append(entity_id)
        failures: set[str] = set()
        for domain, entity_ids in by_domain.items():
            try:
                await self._hass.services.async_call(domain, action, {"entity_id": entity_ids}, blocking=True)
            except HomeAssistantError:
                failures.update(entity_ids)
        changed_ids: list[str] = []
        expected = "off" if action == "turn_off" else "on"
        for outcome in outcomes:
            entity_id = outcome["entity_id"]
            current = self._hass.states.get(entity_id)
            if entity_id not in failures and current is not None and current.state == expected:
                outcome["outcome"] = "changed"
                changed_ids.append(entity_id)
            else:
                outcome["outcome"] = "failed"
        if action == "turn_off":
            await self._hass.async_add_executor_job(now_data.save_restore_set, room_id, changed_ids)
        else:
            # An ON request consumes the captured set once, even when a device was
            # removed or a service failed, so a later tap cannot re-run stale work.
            await self._hass.async_add_executor_job(now_data.consume_restore_set, room_id)
        return {
            "ok": not any(item["outcome"] == "failed" for item in outcomes),
            "room_id": room_id,
            "action": action,
            "outcomes": outcomes,
            "restore_pending_count": len(changed_ids) if action == "turn_off" else 0,
            "restored_from_capture_count": len(restore_ids) if action == "turn_on" else 0,
        }
