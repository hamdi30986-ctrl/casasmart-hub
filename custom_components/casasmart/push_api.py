"""Push endpoints: device push tokens and HQ reminder notifications.

- /api/casasmart/auth/push-token: a paired device registers (POST) or removes
  (DELETE) its FCM token. The device id comes from the caller's token, so a
  device can only manage its own entry.
- /api/casasmart/notifications/hq: HQ's signed reminder requests
  (hq_notifications), each sent as one generic push to the owner.
"""

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

# Per-address limit on HQ requests. Idle addresses are forgotten once more than
# _HQ_RATE_MAX_PEERS are tracked.
_HQ_RATE_LIMIT = 30
_HQ_RATE_WINDOW_SECONDS = 60
_HQ_RATE_MAX_PEERS = 512


def _get_runtime_data(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    """The loaded entry's runtime data, or None while the hub isn't set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _get_push_store(hass: HomeAssistant):
    """The push-token store, or None while the hub isn't set up."""
    runtime = _get_runtime_data(hass)
    return runtime.push if runtime is not None else None


def _hq_delivery_lock(hass: HomeAssistant) -> asyncio.Lock:
    """The HQ delivery lock, shared by every view instance.

    build_views creates views for HA's HTTP port and for the TLS listener
    (again on each TLS refresh). A retry must wait for the first delivery to
    be recorded whichever listener it reaches, so the lock is in hass.data.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "hq_notification_lock", asyncio.Lock()
    )


class CasaSmartPushTokenView(HomeAssistantView):
    """POST and DELETE /api/casasmart/auth/push-token.

    Both need session.manage, which widget tokens lack: a widget must not
    redirect or drop its owner's notifications.
    """

    url = "/api/casasmart/auth/push-token"
    name = "api:casasmart:auth:push-token"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Register or refresh the caller's FCM push token (an upsert)."""
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
    """POST /api/casasmart/notifications/hq: one signed, content-free reminder.

    HQ's Ed25519 signature authenticates the request instead of a CasaSmart
    token. The cheap checks run before the signature check; the nonce, the
    duplicate check and the send run under the shared delivery lock.

    Answers 202 when the relay accepted the push, 200 with duplicate: true for
    an event already delivered, 401 HQ_AUTH_REJECTED for any failed check of
    the signed request (the reason is audited, not returned), 400 for a bad
    content type or size, 429 over the rate limit, and 503 when push is off or
    the delivery failed.
    """

    url = "/api/casasmart/notifications/hq"
    name = "api:casasmart:notifications:hq"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._lock = _hq_delivery_lock(hass)
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _rate_limited(self, peer: str) -> bool:
        """True once peer has used up its requests for the current window."""
        now = time.monotonic()
        window_start = now - _HQ_RATE_WINDOW_SECONDS
        attempts = self._attempts[peer]
        while attempts and attempts[0] <= window_start:
            attempts.popleft()
        if len(attempts) >= _HQ_RATE_LIMIT:
            return True
        attempts.append(now)
        if len(self._attempts) > _HQ_RATE_MAX_PEERS:
            for key in list(self._attempts):
                if not self._attempts[key] or self._attempts[key][-1] <= window_start:
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
        # content.read(n) returns what has arrived so far, so read until EOF.
        raw = b""
        while len(raw) <= HQ_NOTIFICATION_MAX_BODY_BYTES:
            chunk = await request.content.read(
                HQ_NOTIFICATION_MAX_BODY_BYTES + 1 - len(raw)
            )
            if not chunk:
                break
            raw += chunk
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
