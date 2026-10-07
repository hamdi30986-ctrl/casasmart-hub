from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_api import authenticate_request
from .const import DOMAIN
from .hq_notifications import (
    HQ_NOTIFICATION_MAX_BODY_BYTES,
    HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY,
    HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY,
    HqNotificationError,
    HqNotificationVerifier,
    hq_push_title,
)
from .push import MAX_TOKEN_LENGTH, VALID_PLATFORMS
from .push_dispatcher import PRIORITY_NORMAL, PUSH_TYPE_HQ_REMINDER

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def _get_push_store(hass: HomeAssistant):
    """Return the PushTokenStore from runtime data, or None."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.push


def _get_runtime_data(hass: HomeAssistant):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _hq_delivery_lock(hass: HomeAssistant) -> asyncio.Lock:
    """The ONE HQ delivery lock, shared across view instances.

    ``build_views`` constructs fresh view objects for HA's own HTTP app and
    the TLS listener (and again on each daily TLS refresh). A retry of the
    same HQ event must wait for the first delivery to be recorded whichever
    listener it arrives on, so the lock lives in ``hass.data``, never on a
    view.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "hq_notification_lock", asyncio.Lock()
    )


class CasaSmartPushTokenView(HomeAssistantView):
    """POST + DELETE /api/casasmart/auth/push-token."""

    url = "/api/casasmart/auth/push-token"
    name = "api:casasmart:auth:push-token"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Register or refresh an FCM push token."""
        # session.manage, not devices.read: a widget token must not repoint
        # where its owner's notifications go.
        claims, err = authenticate_request(self._hass, request, "session.manage")
        if err is not None:
            return err

        try:
            body = await request.json()
        except (ValueError, KeyError):
            body = None
        if not isinstance(body, dict):
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
        """Unregister the calling device's push token (logout)."""
        claims, err = authenticate_request(self._hass, request, "session.manage")
        if err is not None:
            return err

        store = _get_push_store(self._hass)
        if store is None:
            return web.json_response(
                {"message": "Hub not ready"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        device_id = claims["sub"]

        existed = await self._hass.async_add_executor_job(store.unregister, device_id)

        return web.json_response(
            {"device_id": device_id, "removed": existed},
            status=HTTPStatus.OK,
        )


class CasaSmartHqNotificationView(HomeAssistantView):
    """Accept one signed, content-free reminder wake-up from HQ."""

    url = "/api/casasmart/notifications/hq"
    name = "api:casasmart:notifications:hq"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._lock = _hq_delivery_lock(hass)
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _rate_limited(self, peer: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[peer]
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 30:
            return True
        attempts.append(now)
        if len(self._attempts) > 512:
            for key in list(self._attempts):
                if not self._attempts[key] or self._attempts[key][-1] <= now - 60:
                    self._attempts.pop(key, None)
        return False

    async def post(self, request: web.Request) -> web.Response:
        runtime = _get_runtime_data(self._hass)
        if runtime is None or runtime.push_dispatcher is None:
            return web.json_response(
                {"accepted": False, "code": "PUSH_UNAVAILABLE"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        peer = request.remote if isinstance(request.remote, str) else "unknown"
        if self._rate_limited(peer):
            return web.json_response(
                {"accepted": False, "code": "RATE_LIMITED"},
                status=HTTPStatus.TOO_MANY_REQUESTS,
            )
        if request.content_type != "application/json" or (
            request.content_length is not None
            and request.content_length > HQ_NOTIFICATION_MAX_BODY_BYTES
        ):
            return web.json_response(
                {"accepted": False, "code": "INVALID_REQUEST"},
                status=HTTPStatus.BAD_REQUEST,
            )
        raw = await request.content.read(HQ_NOTIFICATION_MAX_BODY_BYTES + 1)
        if len(raw) > HQ_NOTIFICATION_MAX_BODY_BYTES:
            return web.json_response(
                {"accepted": False, "code": "INVALID_REQUEST"},
                status=HTTPStatus.BAD_REQUEST,
            )
        signed_headers = {
            name: request.headers.get(name, "")
            for name in (
                "X-CasaSmart-HQ-Timestamp",
                "X-CasaSmart-HQ-Nonce",
                "X-CasaSmart-HQ-Signature",
            )
        }
        verifier = HqNotificationVerifier(
            runtime.storage.table("hq_notifications"),
            runtime.hub_config.get(HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY),
        )
        try:
            verified = await self._hass.async_add_executor_job(
                verifier.verify, signed_headers, raw
            )
        except HqNotificationError as err:
            await self._hass.async_add_executor_job(verifier.record_rejection, err.code)
            return web.json_response(
                {"accepted": False, "code": "HQ_AUTH_REJECTED"},
                status=HTTPStatus.UNAUTHORIZED,
            )
        async with self._lock:
            try:
                await self._hass.async_add_executor_job(
                    verifier.reserve_nonce, verified.nonce
                )
            except HqNotificationError as err:
                await self._hass.async_add_executor_job(
                    verifier.record_rejection, err.code
                )
                return web.json_response(
                    {"accepted": False, "code": "HQ_AUTH_REJECTED"},
                    status=HTTPStatus.UNAUTHORIZED,
                )
            previous = await self._hass.async_add_executor_job(
                verifier.previous, verified.event_id
            )
            if previous is not None:
                await self._hass.async_add_executor_job(
                    verifier.record_duplicate, verified.event_id
                )
                return web.json_response(
                    {
                        "accepted": previous.get("outcome") == "relay_accepted",
                        "duplicate": True,
                        "delivery": previous.get("outcome"),
                    },
                    status=HTTPStatus.OK,
                )
            result: dict[str, Any] = await runtime.push_dispatcher.async_send(
                {
                    "type": PUSH_TYPE_HQ_REMINDER,
                    "title": hq_push_title(
                        runtime.hub_config.get(HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY)
                    ),
                    "body": "You have a private update.",
                    "target": "today",
                },
                PRIORITY_NORMAL,
            )
            outcome = str(result.get("delivery", "failed"))
            await self._hass.async_add_executor_job(
                verifier.record_delivery,
                verified.event_id,
                outcome,
                result.get("reason"),
            )
            accepted = outcome == "relay_accepted"
            return web.json_response(
                {"accepted": accepted, "duplicate": False, "delivery": outcome},
                status=(
                    HTTPStatus.ACCEPTED if accepted else HTTPStatus.SERVICE_UNAVAILABLE
                ),
            )
