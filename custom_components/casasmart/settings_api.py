"""GET/PUT /api/casasmart/me/settings: the caller's own settings.

Settings belong to the member behind the token, like /me/favorites, so a
token only reaches its own member's settings. GET needs devices.read and PUT
needs session.manage, so a widget token can read them but not change them.
PUT is a partial update, so the profile screen and the widget editor don't
overwrite each other.
"""

from __future__ import annotations

import logging
import re
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

# Tile types whose entityId is an HA entity, so the served and scope checks
# apply. Other tiles (tank, scene, security) pass through unchecked.
_ENTITY_TILE_TYPES = frozenset({"toggle", "power", "climate"})
# The shape of an HA entity id. A toggle tile may also hold an id of the apps'
# own, such as light_group:<id>, which names no entity and passes through.
_ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


def get_user_settings(hass: HomeAssistant) -> UserSettingsEngine | None:
    """The loaded entry's settings engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.user_settings


class CasaSmartUserSettingsView(HomeAssistantView):
    """GET/PUT /api/casasmart/me/settings."""

    url = f"/api/{DOMAIN}/me/settings"
    name = f"api:{DOMAIN}:me:settings"
    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        settings = get_user_settings(self._hass)
        if settings is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        # A legacy device with no member is its own member.
        engine = get_engine(self._hass)
        sub = claims["sub"]

        def _load() -> dict:
            return settings.get(engine.member_id_for(sub) if engine else sub)

        doc = await self._hass.async_add_executor_job(_load)
        # Filter for the reply only: during HA startup states are still
        # arriving, and saving the filtered list would erase the layout.
        if doc.get("widget_tiles"):
            scope = claims.get("rooms")
            doc["widget_tiles"] = [
                tile for tile in doc["widget_tiles"] if self._tile_allowed(tile, scope)
            ]
        return self.json(doc)

    def _tile_allowed(self, tile: object, scope: list[str] | None) -> bool:
        """False for an entity tile that is missing, unserved or out of scope."""
        if not isinstance(tile, dict) or tile.get("type") not in _ENTITY_TILE_TYPES:
            return True
        eid = tile.get("entityId")
        if isinstance(eid, str) and not _ENTITY_ID.fullmatch(eid):
            return True
        return (
            isinstance(eid, str)
            and self._hass.states.get(eid) is not None
            and is_served(self._hass, eid)
            and in_scope(self._hass, eid, scope)
        )

    async def put(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "session.manage")
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
        # Shape errors, a non-string type included, are left to the engine.
        tiles = payload.get("widget_tiles")
        if isinstance(tiles, list):
            scope = claims.get("rooms")
            for tile in tiles:
                if (
                    isinstance(tile, dict)
                    and isinstance(tile.get("type"), str)
                    and not self._tile_allowed(tile, scope)
                ):
                    return self.json_message(
                        f"Unknown device {tile.get('entityId')!r}",
                        HTTPStatus.BAD_REQUEST,
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
        # The app re-reads its settings on any registry_changed.
        self._hass.bus.async_fire(EVENT_REGISTRY_CHANGED, {"kind": "settings"})
        return self.json(doc)
