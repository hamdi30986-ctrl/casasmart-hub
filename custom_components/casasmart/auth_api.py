"""Auth + pairing + user-management endpoints (Track B — B1.6/B2).

The plan's login chain:

- ``POST /api/casasmart/auth/enroll`` — store a device's P-256 public key
  + name. B2: gated by a **single-use pairing code** (role + room scope
  are baked into the code at generation — the phone never picks its own
  privileges) and **LAN-only** (plan decision 2026-06-10: a leaked
  pairing QR is useless remotely).
- ``POST /api/casasmart/auth/challenge`` — hand out a one-time nonce.
- ``POST /api/casasmart/auth/token`` — verify the signed nonce, mint the
  JWT. Failures are deliberately generic (don't reveal whether the
  device, the nonce, or the signature was the problem) and throttled
  (HTTP 429 + Retry-After, escalating walls).

B2 management surface (JWT-gated through the same engine):

- ``POST/GET /api/casasmart/pairing/codes`` + ``DELETE .../{code_id}`` —
  generate / list / revoke pairing codes (``pairing.generate``, admin).
- ``GET /api/casasmart/users``, ``PATCH/DELETE .../{device_id}`` — list,
  edit (role/room scope), unpair (``users.manage``, admin). Edits and
  unpairs invalidate the target's outstanding JWTs instantly.

``authenticate_request`` is the gate every protected view calls: extracts
the bearer token, validates it against the engine, checks the named
permission. It replaces ``requires_auth = True`` (HA tokens no longer
grant access to CasaSmart endpoints — the plan's "HA token must die").
"""

from __future__ import annotations

import ipaddress
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_engine import (
    AuthEngine,
    ChallengeError,
    EnrollError,
    UnknownDeviceError,
    UserManagementError,
)
from .auth_tokens import ROLE_ADMIN, TokenError
from .const import DOMAIN
from .pairing import CodeInvalidError, PairingError, PairingManager
from .recovery import CodeInvalidError as RecoveryCodeInvalidError
from .recovery import RecoveryManager
from .throttle import ThrottledError

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


def get_pairing(hass: HomeAssistant) -> PairingManager | None:
    """The loaded entry's pairing manager, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.pairing


def get_recovery(hass: HomeAssistant) -> RecoveryManager | None:
    """The loaded entry's recovery manager, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.recovery


def notify_recovery_code(hass: HomeAssistant, code: str) -> None:
    """Surface a freshly minted recovery code to the HA admin (Hamdi).

    The plaintext exists exactly once — here. Hamdi engraves it on the
    metal card; dismissing the notification is the only copy gone.
    """
    persistent_notification.async_create(
        hass,
        f"Owner recovery code: **{code}**\n\n"
        "Engrave this on the recovery card and store it with the owner. "
        "It is single-use — redeeming it replaces the hub's admin and a "
        "new code will be issued.",
        title="CasaSmart Hub — recovery code",
        notification_id=f"{DOMAIN}_recovery_code",
    )


def arm_recovery(hass: HomeAssistant) -> None:
    """Mint the recovery code on a claimed hub (no-op when already armed).

    Blocking storage write — call via executor.
    """
    recovery = get_recovery(hass)
    if recovery is None:
        return
    code = recovery.ensure_armed()
    if code is not None:
        hass.loop.call_soon_threadsafe(notify_recovery_code, hass, code)


def is_lan_request(
    request: web.Request, extra_cidrs: list[str] | None = None
) -> bool:
    """True when the request came from the hub's own network.

    Plan decision 2026-06-10: initial pairing only completes on the LAN.
    Loopback is deliberately EXCLUDED — tunnel traffic (cloudflared)
    reaches HA from localhost, and the whole point is that a leaked
    pairing QR is useless remotely. Link-local/private = LAN; everything
    else (public, loopback, unparseable) is refused.

    ``extra_cidrs`` (hub config ``pairing_extra_lan_cidrs``, default
    unset) is a deployment knob for environments whose port proxy
    rewrites the client source address — e.g. Docker Desktop presents
    every inbound connection as its VM interface IP, which can land in
    public space. Production (HAOS/LXC) sees real peer IPs and never
    needs this.
    """
    try:
        remote = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    if (remote.is_private or remote.is_link_local) and not remote.is_loopback:
        return True
    for cidr in extra_cidrs or []:
        try:
            if remote in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            _LOGGER.error("Ignoring invalid pairing_extra_lan_cidrs entry %r", cidr)
    return False


def get_extra_lan_cidrs(hass: HomeAssistant) -> list[str]:
    """The hub's configured extra pairing CIDRs ([] when unset/malformed)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return []
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    cidrs = runtime_data.hub_config.get("pairing_extra_lan_cidrs")
    if not isinstance(cidrs, list):
        return []
    return [cidr for cidr in cidrs if isinstance(cidr, str)]


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


async def json_body(request: web.Request) -> dict[str, Any] | None:
    """The request body as a dict, or None when it isn't one."""
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class CasaSmartEnrollView(HomeAssistantView):
    """POST /api/casasmart/auth/enroll — pair a device (B2 flow).

    The pairing code IS the credential: role + room scope come from the
    code, never from the request. LAN-only — see ``is_lan_request``.
    """

    url = f"/api/{DOMAIN}/auth/enroll"
    name = f"api:{DOMAIN}:auth:enroll"
    requires_auth = False  # the pairing code is the gate

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        pairing = get_pairing(self._hass)
        if engine is None or pairing is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        if not is_lan_request(request, get_extra_lan_cidrs(self._hass)):
            # Plan: a leaked/photographed pairing QR is useless remotely.
            _LOGGER.warning(
                "Pairing attempt refused (non-LAN source: %s)", request.remote
            )
            return self.json_message(
                "Pairing is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        source = request.remote or "unknown"
        try:
            grant = await self._hass.async_add_executor_job(
                pairing.redeem, payload.get("pairing_code", ""), source
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except CodeInvalidError:
            # One generic bucket: unknown vs expired vs used is not leaked.
            return self.json_message(
                "Invalid pairing code", HTTPStatus.UNAUTHORIZED
            )

        try:
            device_id = await self._hass.async_add_executor_job(
                lambda: engine.enroll_device(
                    name=payload.get("name", ""),
                    role=grant["role"],
                    public_key_pem=payload.get("public_key", ""),
                    rooms=grant["rooms"],
                )
            )
        except EnrollError as err:
            # The code was already consumed (single-use is non-negotiable);
            # a bad name/key costs the code. The admin can mint another.
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        if grant["role"] == ROLE_ADMIN:
            # The hub just got claimed — arm the B3 recovery code so the
            # installer can engrave the card before leaving the site.
            await self._hass.async_add_executor_job(arm_recovery, self._hass)

        return self.json(
            {"device_id": device_id, "role": grant["role"], "rooms": grant["rooms"]},
            HTTPStatus.CREATED,
        )


class CasaSmartRecoverView(HomeAssistantView):
    """POST /api/casasmart/auth/recover — metal-card admin recovery (B3).

    Lost phone + no cloud backup: the owner presents the recovery code
    plus a NEW keypair; the hub swaps its admin (old admin's JWTs die
    instantly) and re-arms with a fresh code. Same posture as enroll:
    the code is the credential, LAN-only, throttled per source.
    """

    url = f"/api/{DOMAIN}/auth/recover"
    name = f"api:{DOMAIN}:auth:recover"
    requires_auth = False  # the recovery code is the gate

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        recovery = get_recovery(self._hass)
        if engine is None or recovery is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        if not is_lan_request(request, get_extra_lan_cidrs(self._hass)):
            # Plan: a photographed card is useless remotely.
            _LOGGER.warning(
                "Recovery attempt refused (non-LAN source: %s)", request.remote
            )
            return self.json_message(
                "Recovery is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        source = request.remote or "unknown"
        try:
            await self._hass.async_add_executor_job(
                recovery.redeem, payload.get("recovery_code", ""), source
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except RecoveryCodeInvalidError:
            # One generic bucket: wrong code vs not-armed is not leaked.
            return self.json_message(
                "Invalid recovery code", HTTPStatus.UNAUTHORIZED
            )

        try:
            device_id = await self._hass.async_add_executor_job(
                lambda: engine.replace_admin(
                    name=payload.get("name", ""),
                    public_key_pem=payload.get("public_key", ""),
                )
            )
        except EnrollError as err:
            # The code is spent (single-use is non-negotiable) but the old
            # admin is untouched — replace_admin validates before swapping.
            # Re-arm so the hub is never left without a recovery path.
            await self._hass.async_add_executor_job(arm_recovery, self._hass)
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        # Fresh code for the next card — surfaced to the HA admin only.
        await self._hass.async_add_executor_job(arm_recovery, self._hass)

        return self.json(
            {"device_id": device_id, "role": "admin", "rooms": None},
            HTTPStatus.CREATED,
        )


class CasaSmartPairingCodesView(HomeAssistantView):
    """POST/GET /api/casasmart/pairing/codes — mint + list pairing codes."""

    url = f"/api/{DOMAIN}/pairing/codes"
    name = f"api:{DOMAIN}:pairing:codes"
    requires_auth = False  # CasaSmart JWT gate below

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "pairing.generate")
        if error is not None:
            return error
        pairing = get_pairing(self._hass)
        if pairing is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        try:
            issued = await self._hass.async_add_executor_job(
                lambda: pairing.generate_code(
                    role=payload.get("role", ""),
                    rooms=payload.get("rooms"),
                    expires_in=payload.get("expires_in", "1d"),
                )
            )
        except PairingError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        _LOGGER.info(
            "Pairing code minted by %s (role=%s)", claims["sub"], issued["role"]
        )
        return self.json(issued, HTTPStatus.CREATED)

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "pairing.generate")
        if error is not None:
            return error
        pairing = get_pairing(self._hass)
        if pairing is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        codes = await self._hass.async_add_executor_job(pairing.list_codes)
        return self.json({"codes": codes})


class CasaSmartPairingCodeView(HomeAssistantView):
    """DELETE /api/casasmart/pairing/codes/{code_id} — revoke a code."""

    url = f"/api/{DOMAIN}/pairing/codes/{{code_id}}"
    name = f"api:{DOMAIN}:pairing:code"
    requires_auth = False  # CasaSmart JWT gate below

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def delete(self, request: web.Request, code_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "pairing.generate")
        if error is not None:
            return error
        pairing = get_pairing(self._hass)
        if pairing is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        revoked = await self._hass.async_add_executor_job(
            pairing.revoke_code, code_id
        )
        if not revoked:
            return self.json_message("Unknown pairing code", HTTPStatus.NOT_FOUND)
        return self.json({"revoked": code_id})


class CasaSmartUsersView(HomeAssistantView):
    """GET /api/casasmart/users — every paired device (admin only)."""

    url = f"/api/{DOMAIN}/users"
    name = f"api:{DOMAIN}:users"
    requires_auth = False  # CasaSmart JWT gate below

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        users = await self._hass.async_add_executor_job(engine.list_devices)
        return self.json({"users": users})


class CasaSmartUserView(HomeAssistantView):
    """PATCH/DELETE /api/casasmart/users/{device_id} — edit or unpair.

    Both paths invalidate the target's outstanding JWTs instantly (the
    engine bumps/drops the device's auth version). The admin record is
    untouchable here — factory reset is the only way out for the owner.
    """

    url = f"/api/{DOMAIN}/users/{{device_id}}"
    name = f"api:{DOMAIN}:user"
    requires_auth = False  # CasaSmart JWT gate below

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def patch(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        if "role" not in payload and "rooms" not in payload:
            return self.json_message(
                "Nothing to change: provide role and/or rooms",
                HTTPStatus.BAD_REQUEST,
            )

        try:
            updated = await self._hass.async_add_executor_job(
                lambda: engine.update_device(
                    device_id,
                    role=payload.get("role"),
                    rooms=payload["rooms"] if "rooms" in payload else ...,
                )
            )
        except UnknownDeviceError:
            return self.json_message("Unknown device", HTTPStatus.NOT_FOUND)
        except UserManagementError as err:
            return self.json_message(str(err), HTTPStatus.FORBIDDEN)

        return self.json(updated)

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            await self._hass.async_add_executor_job(
                engine.delete_device, device_id
            )
        except UnknownDeviceError:
            return self.json_message("Unknown device", HTTPStatus.NOT_FOUND)
        except UserManagementError as err:
            return self.json_message(str(err), HTTPStatus.FORBIDDEN)

        return self.json({"unpaired": device_id})


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
        payload = await json_body(request)
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
        payload = await json_body(request)
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


class CasaSmartWidgetTokenView(HomeAssistantView):
    """POST /api/casasmart/auth/widget-token — mint the widget token.

    B16 3c-3 widgets (option A): home-screen widgets can't run the
    challenge-response login, so the app trades its REGULAR session token
    for a long-lived ``scope: widget`` token and hands THAT to native
    widget storage — the raw HA token never leaves the app process again.

    ``widget.token`` is outside ``WIDGET_SCOPE_PERMISSIONS``, so a widget
    token presented here is refused by ``authorize`` — widget tokens
    cannot self-renew; only a live app session can mint one.
    """

    url = f"/api/{DOMAIN}/auth/widget-token"
    name = f"api:{DOMAIN}:auth:widget-token"
    requires_auth = False  # CasaSmart JWT gate below

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "widget.token")
        if error is not None:
            return error
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            issued = await self._hass.async_add_executor_job(
                engine.mint_widget_token, claims["sub"]
            )
        except UnknownDeviceError:
            # The device vanished between validate and mint — same bucket
            # as an invalid token, nothing to enumerate.
            return self.json_message(
                "Invalid or expired token", HTTPStatus.UNAUTHORIZED
            )

        _LOGGER.info("Widget token minted for %s", claims["sub"])
        return self.json(issued, HTTPStatus.CREATED)


def _throttled_response(err: ThrottledError) -> web.Response:
    return web.json_response(
        {"message": str(err), "retry_after": int(err.retry_after)},
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(int(err.retry_after))},
    )
