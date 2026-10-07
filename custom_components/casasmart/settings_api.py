"""Per-user settings endpoints.

``GET/PUT /api/casasmart/me/settings`` — the caller's own settings doc
(display name + widget layout today), keyed by the person behind the JWT's
``sub`` (its ``member_id``) exactly like ``/me/favorites``: a token can
never read or write another user's settings, which is the whole permission
story for personal data.

GET rides ``devices.read`` and PUT ``session.manage`` — the favorites
posture: every role's session may keep its own settings, but a home-screen
widget's token (device read + control only) can't rewrite them.
PUT is a partial update (only the named fields move; explicit null
clears) so the profile screen and the widget editor write independently
without clobbering each other.
"""

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

# Widget tile types whose entityId IS a live HA entity (so the served + scope
# gate applies); tank/scene/security are pseudo-tiles the hub can't key, so they
# pass through filtering untouched — the same split favorites makes for tanks.
_ENTITY_TILE_TYPES = frozenset({"toggle", "power", "climate"})


def get_user_settings(hass: HomeAssistant) -> UserSettingsEngine | None:
    """The loaded entry's settings engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.user_settings


class CasaSmartUserSettingsView(HomeAssistantView):
    """GET/PUT /api/casasmart/me/settings — the caller's own settings."""

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
        # Settings roam per PERSON: resolve sub -> member_id in the
        # executor; a legacy device is its own member (falls back to sub).
        engine = get_engine(self._hass)
        sub = claims["sub"]

        def _load() -> tuple[str, dict]:
            mid = engine.member_id_for(sub) if engine else sub
            return mid, settings.get(mid)

        _member_id, doc = await self._hass.async_add_executor_job(_load)
        # Widget-tile parity with favorites: filter an absent/unserved ENTITY
        # tile from this response, but do not mutate storage from GET. During
        # HA startup entity states are populated incrementally; persisting the
        # transient filtered view would permanently erase the user's layout.
        # Non-HA pseudo-tiles (tank/scene/security) pass through.
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
        """An ENTITY tile survives only if its HA entity exists + is served; a
        pseudo-tile (no HA entity to gate) always survives."""
        if not isinstance(tile, dict) or tile.get("type") not in _ENTITY_TILE_TYPES:
            return True
        eid = tile.get("entityId")
        return (
            isinstance(eid, str)
            and self._hass.states.get(eid) is not None
            and is_served(self._hass, eid)
        )

    def _tile_in_scope(self, tile: object, scope: object) -> bool:
        """Scope an ENTITY tile to the caller; pseudo-tiles are unscoped."""
        if not isinstance(tile, dict) or tile.get("type") not in _ENTITY_TILE_TYPES:
            return True
        return in_scope(self._hass, tile.get("entityId"), scope)

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
        # Write-guard (favorites parity): an ENTITY tile must point at a
        # served entity in the caller's scope, so a scoped member can't pin
        # another room's device into their widget. Pseudo-tiles pass; shape
        # validation (a non-string type included) stays the engine's job
        # (_clean_widget_tiles).
        tiles = payload.get("widget_tiles")
        if isinstance(tiles, list):
            scope = claims.get("rooms")
            for tile in tiles:
                if (
                    not isinstance(tile, dict)
                    or not isinstance(tile.get("type"), str)
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
        # Nudge the member's other devices — the app re-pulls its
        # settings on any registry_changed.
        self._hass.bus.async_fire(EVENT_REGISTRY_CHANGED, {"kind": "settings"})
        return self.json(doc)
