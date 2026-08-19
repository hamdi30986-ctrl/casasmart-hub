"""CasaSmart runtime component."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_api import authenticate_request, get_engine, json_body
from .const import DOMAIN, EVENT_REGISTRY_CHANGED
from .filtering import in_scope, is_served
from .user_settings import SettingsError, UserSettingsEngine

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)




_ENTITY_TILE_TYPES = frozenset({"toggle", "power", "climate"})


def get_user_settings(hass: HomeAssistant) -> UserSettingsEngine | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.user_settings


class CasaSmartUserSettingsView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/me/settings"
    name = f"api:{DOMAIN}:me:settings"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        settings = get_user_settings(self._hass)
        if settings is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)


        engine = get_engine(self._hass)
        sub = claims["sub"]

        def _load() -> tuple[str, dict]:
            mid = engine.member_id_for(sub) if engine else sub
            return mid, settings.get(mid)

        member_id, doc = await self._hass.async_add_executor_job(_load)





        tiles = doc.get("widget_tiles")
        if tiles:
            served = [t for t in tiles if self._tile_alive(t)]
            doc["widget_tiles"] = served
            scope = claims.get("rooms")
            doc["widget_tiles"] = [
                t for t in doc["widget_tiles"] if self._tile_in_scope(t, scope)
            ]
        return self.json(doc)

    def _tile_alive(self, tile: object) -> bool:
        """CasaSmart runtime component."""
        if not isinstance(tile, dict) or tile.get("type") not in _ENTITY_TILE_TYPES:
            return True
        eid = tile.get("entityId")
        return (
            isinstance(eid, str)
            and self._hass.states.get(eid) is not None
            and is_served(self._hass, eid)
        )

    def _tile_in_scope(self, tile: object, scope: object) -> bool:
        """CasaSmart runtime component."""
        if not isinstance(tile, dict) or tile.get("type") not in _ENTITY_TILE_TYPES:
            return True
        return in_scope(self._hass, tile.get("entityId"), scope)

    async def put(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "devices.control"
        )
        if error is not None:
            return error
        settings = get_user_settings(self._hass)
        if settings is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )




        tiles = payload.get("widget_tiles")
        if isinstance(tiles, list):
            scope = claims.get("rooms")
            for tile in tiles:
                if (
                    not isinstance(tile, dict)
                    or tile.get("type") not in _ENTITY_TILE_TYPES
                ):
                    continue
                eid = tile.get("entityId")
                if (
                    not isinstance(eid, str)
                    or self._hass.states.get(eid) is None
                    or not is_served(self._hass, eid)
                    or not in_scope(self._hass, eid, scope)
                ):
                    return self.json_message(
                        f"Unknown device {eid!r}", HTTPStatus.BAD_REQUEST
                    )
        engine = get_engine(self._hass)
        sub = claims["sub"]
        try:
            doc = await self._hass.async_add_executor_job(
                lambda: settings.update(
                    engine.member_id_for(sub) if engine else sub, payload
                )
            )
        except SettingsError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)


        self._hass.bus.async_fire(EVENT_REGISTRY_CHANGED, {"kind": "settings"})
        return self.json(doc)
