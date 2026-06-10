"""Auth endpoints + the request gate (Track B — B1.6).

Three endpoints implement the plan's login chain:

- ``POST /api/casasmart/auth/enroll`` — store a device's P-256 public key
  + name + role. Gated by HA's bearer auth as the B1.6 provisioning
  stopgap; B2 replaces that gate with single-use pairing codes (the
  engine call does not change).
- ``POST /api/casasmart/auth/challenge`` — hand out a one-time nonce.
- ``POST /api/casasmart/auth/token`` — verify the signed nonce, mint the
  JWT. Failures are deliberately generic (don't reveal whether the
  device, the nonce, or the signature was the problem) and throttled
  (HTTP 429 + Retry-After after 5 failures).

``authenticate_request`` is the gate every protected view calls: extracts
the bearer token, validates it against the engine, checks the named
permission. It replaces ``requires_auth = True`` (HA tokens no longer
grant access to CasaSmart endpoints — the plan's "HA token must die").
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_engine import (
    AdminExistsError,
    AuthEngine,
    ChallengeError,
    EnrollError,
    ThrottledError,
    UnknownDeviceError,
)
from .auth_tokens import TokenError
from .const import DOMAIN

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def get_engine(hass: HomeAssistant) -> AuthEngine | None:
    """The loaded entry's auth engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.auth


def authenticate_request(
    hass: HomeAssistant, request: web.Request, permission: str
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Validate the CasaSmart JWT on a request and check one permission.

    Returns ``(claims, None)`` on success or ``(None, error_response)``
    to return as-is. Token validation is pure HMAC math — safe on the
    event loop, no executor hop per request.
    """
    engine = get_engine(hass)
    if engine is None:
        return None, web.json_response(
            {"message": "Hub not ready"}, status=HTTPStatus.SERVICE_UNAVAILABLE
        )

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None, web.json_response(
            {"message": "Missing bearer token"},
            status=HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = engine.validate_token(authorization.removeprefix("Bearer "))
    except TokenError:
        return None, web.json_response(
            {"message": "Invalid or expired token"},
            status=HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not AuthEngine.authorize(claims, permission):
        return None, web.json_response(
            {"message": "Forbidden"}, status=HTTPStatus.FORBIDDEN
        )
    return claims, None


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    """The request body as a dict, or None when it isn't one."""
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class CasaSmartEnrollView(HomeAssistantView):
    """POST /api/casasmart/auth/enroll — register a device identity.

    HA bearer auth is the provisioning gate until B2's pairing codes.
    """

    url = f"/api/{DOMAIN}/auth/enroll"
    name = f"api:{DOMAIN}:auth:enroll"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload = await _json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        try:
            device_id = await self._hass.async_add_executor_job(
                lambda: engine.enroll_device(
                    name=payload.get("name", ""),
                    role=payload.get("role", ""),
                    public_key_pem=payload.get("public_key", ""),
                    rooms=payload.get("rooms"),
                )
            )
        except AdminExistsError as err:
            return self.json_message(str(err), HTTPStatus.CONFLICT)
        except EnrollError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        return self.json({"device_id": device_id}, HTTPStatus.CREATED)


class CasaSmartChallengeView(HomeAssistantView):
    """POST /api/casasmart/auth/challenge — a one-time nonce to sign."""

    url = f"/api/{DOMAIN}/auth/challenge"
    name = f"api:{DOMAIN}:auth:challenge"
    requires_auth = False  # this IS the start of authentication

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload = await _json_body(request)
        device_id = (payload or {}).get("device_id")
        if not isinstance(device_id, str) or not device_id:
            return self.json_message("device_id is required", HTTPStatus.BAD_REQUEST)

        try:
            challenge = await self._hass.async_add_executor_job(
                engine.create_challenge, device_id
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except UnknownDeviceError:
            return self.json_message("Unknown device", HTTPStatus.NOT_FOUND)

        return self.json(challenge)


class CasaSmartTokenView(HomeAssistantView):
    """POST /api/casasmart/auth/token — signed nonce in, JWT out."""

    url = f"/api/{DOMAIN}/auth/token"
    name = f"api:{DOMAIN}:auth:token"
    requires_auth = False  # the signature IS the credential

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload = await _json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        device_id = payload.get("device_id")
        challenge_id = payload.get("challenge_id")
        signature = payload.get("signature")
        if not all(isinstance(value, str) and value for value in
                   (device_id, challenge_id, signature)):
            return self.json_message(
                "device_id, challenge_id and signature are required",
                HTTPStatus.BAD_REQUEST,
            )

        try:
            issued = await self._hass.async_add_executor_job(
                engine.redeem_challenge, device_id, challenge_id, signature
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except ChallengeError as err:
            # Deliberately generic — no hint which part failed.
            return self.json_message(str(err), HTTPStatus.UNAUTHORIZED)

        return self.json(issued)


def _throttled_response(err: ThrottledError) -> web.Response:
    return web.json_response(
        {"message": str(err), "retry_after": int(err.retry_after)},
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(int(err.retry_after))},
    )
