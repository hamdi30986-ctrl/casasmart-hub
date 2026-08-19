"""CasaSmart runtime component."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_api import authenticate_request
from .const import DOMAIN
from .push import MAX_TOKEN_LENGTH, VALID_PLATFORMS

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def _get_push_store(hass: HomeAssistant):
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.push


class CasaSmartPushTokenView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = "/api/casasmart/auth/push-token"
    name = "api:casasmart:auth:push-token"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """CasaSmart runtime component."""
        claims, err = authenticate_request(
            self._hass, request, "devices.read"
        )
        if err is not None:
            return err

        try:
            body = await request.json()
        except (ValueError, KeyError):
            return web.json_response(
                {"message": "Invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )

        fcm_token = body.get("fcm_token")
        platform = body.get("platform")

        if not fcm_token or not isinstance(fcm_token, str):
            return web.json_response(
                {"message": "fcm_token is required (string)"},
                status=HTTPStatus.BAD_REQUEST,
            )
        if not fcm_token.strip():
            return web.json_response(
                {"message": "fcm_token must not be blank"},
                status=HTTPStatus.BAD_REQUEST,
            )
        if len(fcm_token) > MAX_TOKEN_LENGTH:
            return web.json_response(
                {"message": f"fcm_token exceeds maximum length of {MAX_TOKEN_LENGTH}"},
                status=HTTPStatus.BAD_REQUEST,
            )
        if platform not in VALID_PLATFORMS:
            return web.json_response(
                {"message": f"platform must be one of {sorted(VALID_PLATFORMS)}"},
                status=HTTPStatus.BAD_REQUEST,
            )

        store = _get_push_store(self._hass)
        if store is None:
            return web.json_response(
                {"message": "Hub not ready"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        device_id = claims["sub"]

        await self._hass.async_add_executor_job(
            store.register, device_id, fcm_token, platform
        )

        return web.json_response(
            {"device_id": device_id, "registered": True},
            status=HTTPStatus.OK,
        )

    async def delete(self, request: web.Request) -> web.Response:
        """CasaSmart runtime component."""
        claims, err = authenticate_request(
            self._hass, request, "devices.read"
        )
        if err is not None:
            return err

        store = _get_push_store(self._hass)
        if store is None:
            return web.json_response(
                {"message": "Hub not ready"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        device_id = claims["sub"]

        existed = await self._hass.async_add_executor_job(
            store.unregister, device_id
        )

        return web.json_response(
            {"device_id": device_id, "removed": existed},
            status=HTTPStatus.OK,
        )
