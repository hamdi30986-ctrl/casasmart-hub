"""Per-user settings endpoints (mini-block MB-2).

``GET/PUT /api/casasmart/me/settings`` — the caller's own settings doc
(display name + widget layout today), keyed by the JWT's ``sub`` exactly
like ``/me/favorites``: a token can never read or write another user's
settings, which is the whole B17 permission story for personal data.

GET rides ``devices.read`` and PUT ``devices.control`` — the favorites
posture: every current role may keep its own settings, but a future
read-only role must not slip through a read permission into a mutation.
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

from .auth_api import authenticate_request, json_body
from .const import DOMAIN
from .user_settings import SettingsError, UserSettingsEngine

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


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
        doc = await self._hass.async_add_executor_job(
            settings.get, claims["sub"]
        )
        return self.json(doc)

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
        try:
            doc = await self._hass.async_add_executor_job(
                settings.update, claims["sub"], payload
            )
        except SettingsError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(doc)
