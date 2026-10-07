"""Auth, pairing and user-management endpoints, and the request gates.

A device pairs with a pairing code (enroll), then logs in by signing a nonce
(challenge, token). The owner can recover the hub with the recovery card, and
the admin mints pairing codes and manages paired devices. authenticate_request
is the CasaSmart token check every protected view calls; views set
requires_auth = False because Home Assistant tokens grant no access here.
is_lan_request is the LAN gate for pairing, recovery and keyless speaker
provisioning.
"""

from __future__ import annotations

import ipaddress
import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_engine import (
    MAX_DEVICE_NAME_LENGTH,
    AuthEngine,
    ChallengeError,
    EnrollError,
    UnknownDeviceError,
    UserManagementError,
)
from .auth_tokens import ROLE_ADMIN, TokenError
from .const import (
    BOOTSTRAP_CODE_HASH_CONFIG_KEY,
    CONF_TUNNEL_ENABLED,
    DOMAIN,
    EVENT_AUTH_CHANGED,
    KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY,
    PROVISION_SECRET_CONFIG_KEY,
    REMOTE_PAIRING_ENABLED_CONFIG_KEY,
)
from .pairing import (
    CodeInvalidError,
    HubAlreadyClaimedError,
    LanOnlyCodeError,
    PairingError,
    PairingManager,
    hash_code,
)
from .recovery import CodeInvalidError as RecoveryCodeInvalidError
from .recovery import RecoveryManager
from .runtime_lookup import loaded_entry, loaded_runtime_data
from .storage import ConfigError
from .throttle import ThrottledError
from .tls import TLS_LISTENER_TRUSTED_LAN
from .tunnel import TUNNEL_URL_CONFIG_KEY, normalize_tunnel_url

_LOGGER = logging.getLogger(__name__)


# Headers Cloudflare adds to every request it proxies. A LAN client that sends
# them only makes itself look remote, which is safe.
_CLOUDFLARE_HEADERS = ("CF-Connecting-IP", "CF-Ray")


# Pairing payload version; v2 adds the fields from _payload_v2_fields.
PAIRING_PAYLOAD_VERSION = 2


# The app's deep links are casasmart://<type>?code=...; admin-minted codes
# are family invites.
_DEEP_LINK_BASE = "casasmart://family"


def get_push_store(hass: HomeAssistant):
    """The loaded entry's push-token store, or None when not set up."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.push if runtime_data is not None else None


def _get_push_dispatcher(hass: HomeAssistant):
    """The loaded entry's push dispatcher, or None when push isn't running."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.push_dispatcher if runtime_data is not None else None


def get_engine(hass: HomeAssistant) -> AuthEngine | None:
    """The loaded entry's auth engine, or None when not set up."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.auth if runtime_data is not None else None


async def async_member_id(hass: HomeAssistant, claims: dict[str, Any]) -> str:
    """The member behind the token; a device with no member is its own.

    Reads storage in the executor, so it may raise StorageError or
    sqlite3.Error.
    """
    engine = get_engine(hass)
    if engine is None:
        return claims["sub"]
    return await hass.async_add_executor_job(engine.member_id_for, claims["sub"])


def get_pairing(hass: HomeAssistant) -> PairingManager | None:
    """The loaded entry's pairing manager, or None when not set up."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.pairing if runtime_data is not None else None


def get_recovery(hass: HomeAssistant) -> RecoveryManager | None:
    """The loaded entry's recovery manager, or None when not set up."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.recovery if runtime_data is not None else None


def get_provision_secret(hass: HomeAssistant) -> str | None:
    """The shared speaker-provisioning key, or None when not set up.

    A speaker sends it in the X-CasaSmart-Provision-Key header on
    GET /audio/provision to fetch its broker settings from any address.
    """
    runtime_data = loaded_runtime_data(hass)
    if runtime_data is None:
        return None
    return runtime_data.hub_config.get(PROVISION_SECRET_CONFIG_KEY)


def is_keyless_speaker_provisioning_enabled(hass: HomeAssistant) -> bool:
    """Whether keyless speaker provisioning is on in hub config (default off).

    When on, GET /audio/provision also serves a LAN client without the
    provisioning key. Only a literal true turns it on.
    """
    runtime_data = loaded_runtime_data(hass)
    return (
        runtime_data is not None
        and runtime_data.hub_config.get(KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY) is True
    )


def notify_recovery_code(hass: HomeAssistant, code: str) -> None:
    """Show a freshly minted recovery code to the HA admin.

    The notification is the only plaintext copy: the installer engraves the
    code on the recovery card, and dismissing the notification deletes it.
    """
    persistent_notification.async_create(
        hass,
        f"Owner recovery code: **{code}**\n\n"
        "Engrave this on the recovery card and store it with the owner. "
        "It is permanent and reusable (LAN-only) — redeeming it re-installs "
        "the owner's phone as admin, and the same card keeps working. A "
        "factory reset replaces it with a new code.",
        title="CasaSmart Hub — recovery code",
        notification_id=f"{DOMAIN}_recovery_code",
    )


def arm_recovery(hass: HomeAssistant) -> None:
    """Mint the recovery code on a claimed hub and show it to the HA admin.

    No-op when a code is already armed. Blocking: run it in the executor, and
    only once an admin exists (on a hub without one, ensure_armed drops the
    code).
    """
    recovery = get_recovery(hass)
    if recovery is None:
        return
    try:
        code = recovery.ensure_armed()
    except ConfigError as err:
        # The device is already paired; this only leaves the card unarmed.
        _LOGGER.warning("Recovery code not armed: could not save it (%s)", err)
        return
    if code is not None:
        hass.loop.call_soon_threadsafe(notify_recovery_code, hass, code)


def _arrived_on_trusted_lan_ingress(request: web.Request) -> bool:
    """Whether the TLS listener served the request while trusted as LAN."""
    app = getattr(request, "app", None)
    try:
        return app is not None and app.get(TLS_LISTENER_TRUSTED_LAN) is True
    except (AttributeError, TypeError):
        return False


def _arrived_through_cloudflare(request: web.Request) -> bool:
    """Whether the request carries a header Cloudflare adds when proxying."""
    headers = request.headers
    if any(name in headers for name in _CLOUDFLARE_HEADERS):
        return True
    return "cloudflare" in str(headers.get("CDN-Loop", "")).lower()


def is_lan_request(request: web.Request) -> bool:
    """Whether the request came from the hub's own network.

    The LAN gate for pairing, owner recovery and keyless speaker provisioning.
    Private and link-local addresses count. Loopback doesn't, because tunnel
    traffic (cloudflared) reaches HA from localhost. A request with Cloudflare
    proxy headers is never LAN, whatever its source address: a tunnel pointed
    at the TLS listener arrives from a private Docker or add-on address. With
    lan_relay_ingress on, arriving on the TLS listener is the proof instead
    (see lan_ingress.py).
    """
    if _arrived_through_cloudflare(request):
        return False
    if _arrived_on_trusted_lan_ingress(request):
        return True
    try:
        remote = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    return (remote.is_private or remote.is_link_local) and not remote.is_loopback


def is_remote_pairing_enabled(hass: HomeAssistant) -> bool:
    """Whether remote pairing is on in hub config (default off).

    When on, the enroll gate lets off-LAN requests through to pairing.redeem,
    which still keeps the bootstrap owner claim LAN-only. Only a literal true
    turns it on.
    """
    runtime_data = loaded_runtime_data(hass)
    return (
        runtime_data is not None
        and runtime_data.hub_config.get(REMOTE_PAIRING_ENABLED_CONFIG_KEY) is True
    )


def authenticate_request(
    hass: HomeAssistant, request: web.Request, permission: str
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Validate the CasaSmart JWT on a request and check one permission.

    Returns (claims, None) on success or (None, error_response) to send back.
    Validation is in-memory HMAC work, so it runs on the event loop.
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
    except TokenError as err:
        # One message for every failure; the code tells the app whether a new
        # login fixes it or the device must pair again (unenrolled).
        return None, web.json_response(
            {"message": "Invalid or expired token", "code": err.code},
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


async def read_json_object(
    view: HomeAssistantView, request: web.Request
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """(body, None) when the body is a JSON object, else (None, the view's 400)."""
    payload = await json_body(request)
    if payload is None:
        return None, view.json_message(
            "Body must be a JSON object", HTTPStatus.BAD_REQUEST
        )
    return payload, None


def ready_or_503[T](
    view: HomeAssistantView, engine: T | None
) -> tuple[T | None, web.Response | None]:
    """(engine, None), or (None, the view's 503) while the hub isn't loaded."""
    if engine is None:
        return None, view.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
    return engine, None


def _throttled_response(err: ThrottledError) -> web.Response:
    """HTTP 429 with the lockout's remaining seconds (body and Retry-After)."""
    return web.json_response(
        {"message": str(err), "retry_after": int(err.retry_after)},
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(int(err.retry_after))},
    )


def _payload_v2_fields(hass: HomeAssistant, code: str) -> dict[str, Any]:
    """The pairing payload v2 fields added to a freshly minted code.

    They let a new phone find and verify the hub without mDNS:
    identity_fingerprint is the TLS identity the app pins (the handshake
    serves the same value) and tunnel_url the remote path. A field that isn't
    available is left out, and the app falls back to LAN-only pairing. A v1
    app reads only the code parameter of qr_payload, so a v2 QR still pairs it.
    """
    fields: dict[str, Any] = {"payload_version": PAIRING_PAYLOAD_VERSION}
    # code comes first, as in the app's v1 link. Only the tunnel URL needs
    # percent-encoding: codes are alphanumeric and the fingerprint is hex.
    params = [("code", code), ("v", str(PAIRING_PAYLOAD_VERSION))]

    entry = loaded_entry(hass)
    if entry is not None:
        runtime_data = entry.runtime_data
        tls = getattr(runtime_data, "tls", None)
        if tls is not None:
            fingerprint = tls.material.identity_fingerprint
            fields["identity_fingerprint"] = fingerprint
            params.append(("fp", fingerprint))
        # Don't send a new phone to a tunnel the owner switched off (unset
        # counts as off). Paired phones still get the URL from the handshake.
        tunnel_on = bool(entry.options.get(CONF_TUNNEL_ENABLED, False))
        tunnel_url = normalize_tunnel_url(
            runtime_data.hub_config.get(TUNNEL_URL_CONFIG_KEY)
        )
        if tunnel_on and tunnel_url is not None:
            fields["tunnel_url"] = tunnel_url
            params.append(("tunnel", quote(tunnel_url, safe="")))

    query = "&".join(f"{key}={value}" for key, value in params)
    fields["qr_payload"] = f"{_DEEP_LINK_BASE}?{query}"
    return fields


class CasaSmartEnrollView(HomeAssistantView):
    """POST /api/casasmart/auth/enroll: pair a device with a pairing code.

    The code sets the role and room scope; the request can't change them.
    Pairing is LAN-only unless remote_pairing_enabled is on; then member
    codes work from anywhere, but the bootstrap owner claim stays LAN-only
    (pairing.redeem enforces that).
    """

    url = f"/api/{DOMAIN}/auth/enroll"
    name = f"api:{DOMAIN}:auth:enroll"
    requires_auth = False  # the pairing code is the gate

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _sticker_hash(self) -> str | None:
        """The stored hash of this hub's permanent bootstrap code, or None.

        It still identifies this hub's own code after the live code is
        dropped on claim, so the owner can re-run onboarding on their hub.
        """
        runtime_data = loaded_runtime_data(self._hass)
        if runtime_data is None:
            return None
        stored = runtime_data.hub_config.get(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
        return stored if isinstance(stored, str) and stored else None

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        pairing = get_pairing(self._hass)
        if engine is None or pairing is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        lan_source = is_lan_request(request)
        if not lan_source and not is_remote_pairing_enabled(self._hass):
            # A leaked pairing QR is useless remotely unless remote pairing is on.
            _LOGGER.warning(
                "Pairing attempt refused (non-LAN source: %s)", request.remote
            )
            return self.json_message(
                "Pairing is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error

        # A paired phone (same key) that re-runs onboarding gets its identity
        # back without redeeming the code; it still has to log in with its
        # private key. The code must still be this hub's, or a phone known to
        # two hubs on one LAN could end up paired to the wrong one.
        existing = await self._hass.async_add_executor_job(
            engine.device_for_public_key, payload.get("public_key", "")
        )
        if existing is not None:
            # Kept out of the response: it only lets this device re-pair with
            # the code it originally redeemed.
            own_code_hash = existing.pop("enrolled_code_hash", None)
            allowed = tuple(h for h in (self._sticker_hash(), own_code_hash) if h)
            try:
                await self._hass.async_add_executor_job(
                    lambda: pairing.authorize_known_device(
                        payload.get("pairing_code", ""),
                        request.remote or "unknown",
                        not lan_source,
                        allowed_hashes=allowed,
                    )
                )
            except ThrottledError as err:
                return _throttled_response(err)
            except CodeInvalidError:
                # Same answer as redeem gives, so the key's status isn't revealed.
                return self.json_message(
                    "Invalid pairing code", HTTPStatus.UNAUTHORIZED
                )
            return self.json(existing, HTTPStatus.CREATED)

        # Check the name and key before redeem consumes the code: a spent
        # sticker code stays gone until the next restart.
        try:
            engine.check_enrollment(
                payload.get("name", ""), payload.get("public_key", "")
            )
        except EnrollError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        source = request.remote or "unknown"
        try:
            grant = await self._hass.async_add_executor_job(
                lambda: pairing.redeem(
                    payload.get("pairing_code", ""),
                    source,
                    remote_source=not lan_source,
                )
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except LanOnlyCodeError:
            # The bootstrap owner claim from off the LAN. The code isn't
            # consumed, so the owner can still claim the hub on the LAN.
            _LOGGER.warning(
                "Pairing refused: LAN-only code class from remote source %s",
                request.remote,
            )
            return self.json_message(
                "Pairing is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        except HubAlreadyClaimedError:
            # The right owner code, from another phone, on a claimed hub.
            return self.json_message("This hub is already paired", HTTPStatus.CONFLICT)
        except CodeInvalidError:
            # Unknown, expired and used codes get the same answer.
            return self.json_message("Invalid pairing code", HTTPStatus.UNAUTHORIZED)

        try:
            device_id = await self._hass.async_add_executor_job(
                lambda: engine.enroll_device(
                    name=payload.get("name", ""),
                    role=grant["role"],
                    public_key_pem=payload.get("public_key", ""),
                    rooms=grant["rooms"],
                    enrolled_via=grant.get("code_id"),
                    member_id=grant.get("member_id"),
                    code_hash=hash_code(payload.get("pairing_code", "")),
                )
            )
        except EnrollError as err:
            # Rare, since name and key were checked first (the single-admin
            # rule losing a race). The code stays spent.
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        if grant["role"] == ROLE_ADMIN:
            # The hub is now claimed: arm the recovery code so the installer
            # can engrave the card before leaving the site.
            await self._hass.async_add_executor_job(arm_recovery, self._hass)

        # Refresh the per-user sensors.
        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})

        # Tell the owner a new device paired, without making the response wait
        # on the relay. The name is trimmed and capped as the engine stores it.
        dispatcher = _get_push_dispatcher(self._hass)
        if dispatcher is not None:
            stored_name = (
                payload.get("name", "").strip()[:MAX_DEVICE_NAME_LENGTH].strip()
            )
            self._hass.async_create_task(
                dispatcher.async_send_device_paired(
                    stored_name, grant["role"], device_id
                )
            )

        return self.json(
            {"device_id": device_id, "role": grant["role"], "rooms": grant["rooms"]},
            HTTPStatus.CREATED,
        )


class CasaSmartRecoverView(HomeAssistantView):
    """POST /api/casasmart/auth/recover: owner recovery with the recovery card.

    For an owner who lost their phone: the recovery code and a new keypair
    make the new phone the admin, and the old admin's tokens stop working.
    The card stays valid. LAN-only, and throttled per source.
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
        if not is_lan_request(request):
            # A photographed card is useless remotely.
            _LOGGER.warning(
                "Recovery attempt refused (non-LAN source: %s)", request.remote
            )
            return self.json_message(
                "Recovery is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error

        source = request.remote or "unknown"
        try:
            await self._hass.async_add_executor_job(
                recovery.redeem, payload.get("recovery_code", ""), source
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except RecoveryCodeInvalidError:
            # A wrong code and an unarmed hub get the same answer.
            return self.json_message("Invalid recovery code", HTTPStatus.UNAUTHORIZED)

        replaced = [
            device["device_id"]
            for device in await self._hass.async_add_executor_job(engine.list_devices)
            if device["role"] == ROLE_ADMIN
        ]
        try:
            device_id = await self._hass.async_add_executor_job(
                lambda: engine.replace_admin(
                    name=payload.get("name", ""),
                    public_key_pem=payload.get("public_key", ""),
                )
            )
        except EnrollError as err:
            # replace_admin validates before swapping, so nothing changed. No
            # arm_recovery here: on a hub with no admin it would drop the card.
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        # Drop the replaced phone's push token, as any unpair does, or it keeps
        # getting the alerts sent to every device (alarms included).
        push = get_push_store(self._hass)
        if push is not None:
            for old_device_id in replaced:
                await self._hass.async_add_executor_job(push.unregister, old_device_id)

        # Mints a code only if none is armed; the swap left an admin in place.
        await self._hass.async_add_executor_job(arm_recovery, self._hass)

        # The admin device changed: refresh the per-user sensors.
        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})

        return self.json(
            {"device_id": device_id, "role": "admin", "rooms": None},
            HTTPStatus.CREATED,
        )


class CasaSmartChallengeView(HomeAssistantView):
    """POST /api/casasmart/auth/challenge: issue a one-time nonce to sign."""

    url = f"/api/{DOMAIN}/auth/challenge"
    name = f"api:{DOMAIN}:auth:challenge"
    requires_auth = False  # this starts authentication

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
                engine.create_challenge, device_id, request.remote or "unknown"
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except UnknownDeviceError:
            return self.json_message("Unknown device", HTTPStatus.NOT_FOUND)

        return self.json(challenge)


class CasaSmartTokenView(HomeAssistantView):
    """POST /api/casasmart/auth/token: exchange a signed nonce for a token."""

    url = f"/api/{DOMAIN}/auth/token"
    name = f"api:{DOMAIN}:auth:token"
    requires_auth = False  # the signature is the credential

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        device_id = payload.get("device_id")
        challenge_id = payload.get("challenge_id")
        signature = payload.get("signature")
        if not all(
            isinstance(value, str) and value
            for value in (device_id, challenge_id, signature)
        ):
            return self.json_message(
                "device_id, challenge_id and signature are required",
                HTTPStatus.BAD_REQUEST,
            )

        try:
            issued = await self._hass.async_add_executor_job(
                engine.redeem_challenge,
                device_id,
                challenge_id,
                signature,
                request.remote or "unknown",
            )
        except ThrottledError as err:
            return _throttled_response(err)
        except ChallengeError as err:
            # The message doesn't say which part failed.
            return self.json_message(str(err), HTTPStatus.UNAUTHORIZED)

        return self.json(issued)


class CasaSmartWidgetTokenView(HomeAssistantView):
    """POST /api/casasmart/auth/widget-token: mint a widget token.

    Home-screen widgets can't run the challenge-response login, so the app
    trades its session token for a long-lived one that can only read and
    control devices, and stops working with the device's next unpair or
    role or room edit. A widget token can't mint another (widget.token is
    outside WIDGET_SCOPE_PERMISSIONS); only a live app session can.
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
            # Unpaired since the token was validated: answer as for a bad token.
            return self.json_message(
                "Invalid or expired token", HTTPStatus.UNAUTHORIZED
            )

        _LOGGER.info("Widget token minted for %s", claims["sub"])
        return self.json(issued, HTTPStatus.CREATED)


class CasaSmartWhoamiView(HomeAssistantView):
    """GET /api/casasmart/auth/whoami: is the caller's device still paired?

    The app calls this on resume. It answers only for the device the token
    names, and ignores expiry and edits: a stale token for a paired device
    gets enrolled: true with the current role and name, so the app logs in
    again instead of pairing again. enrolled: false (unpaired or forged)
    sends the app back to pairing. Always 200, so the app reads one boolean.
    """

    url = f"/api/{DOMAIN}/auth/whoami"
    name = f"api:{DOMAIN}:auth:whoami"
    requires_auth = False  # the handler reads the bearer token itself

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return self.json({"enrolled": False})

        device = await self._hass.async_add_executor_job(
            engine.device_for_token, authorization.removeprefix("Bearer ")
        )
        if device is None:
            return self.json({"enrolled": False})
        return self.json(
            {
                "enrolled": True,
                "role": device["role"],
                "device_id": device["device_id"],
                "name": device.get("name"),
            }
        )


class CasaSmartUnpairSelfView(HomeAssistantView):
    """POST /api/casasmart/auth/unpair-self: the calling device leaves the hub.

    "Remove Hub" in the app calls this, so an owner can hand the hub back
    without the recovery card or a reset. The token proves possession of the
    device's private key, so any role, the admin included, may remove itself.
    Only the token's own device is unpaired; the body can't name another.
    When the last admin leaves, the permanent sticker code is armed again
    from its stored hash, so the owner can claim the hub with the printed code.
    """

    url = f"/api/{DOMAIN}/auth/unpair-self"
    name = f"api:{DOMAIN}:auth:unpair-self"
    requires_auth = False  # CasaSmart JWT gate below

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        # Every role holds session.manage but widget tokens don't, so a widget
        # can't unpair the device that minted it.
        claims, error = authenticate_request(self._hass, request, "session.manage")
        if error is not None:
            return error
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        device_id = claims["sub"]
        try:
            member_id = await self._hass.async_add_executor_job(
                engine.leave_hub, device_id
            )
        except UnknownDeviceError:
            # Already gone: a retry after a lost response must not look like a
            # failure.
            return self.json({"unpaired": device_id, "hub_unclaimed": False})

        push = get_push_store(self._hass)
        if push is not None:
            await self._hass.async_add_executor_job(push.unregister, device_id)

        runtime = loaded_runtime_data(self._hass)
        unclaimed = False
        if runtime is not None:

            def _finish_leave() -> bool:
                # As with an admin unpair, a member's data goes with their last
                # device.
                if engine.member_device_count(member_id) == 0:
                    runtime.registry.delete_favorites(member_id)
                    runtime.user_settings.delete(member_id)
                if engine.has_admin():
                    return False
                # Last admin gone: arm the sticker code again from its stored
                # hash, so the owner can claim the hub with the printed code.
                code_hash = runtime.hub_config.get(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
                if not code_hash:
                    # A new random code would be one nobody can read, so only
                    # log it; a reset from Home Assistant re-claims the hub.
                    _LOGGER.warning(
                        "Last admin left but no stored bootstrap hash — "
                        "re-claim needs the hub's reset button"
                    )
                    return True
                runtime.pairing.install_bootstrap_hash(code_hash)
                _LOGGER.info(
                    "Last admin left — hub is unclaimed and the permanent "
                    "pairing code is armed again"
                )
                return True

            unclaimed = await self._hass.async_add_executor_job(_finish_leave)

        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})
        return self.json({"unpaired": device_id, "hub_unclaimed": unclaimed})


class CasaSmartPairingCodesView(HomeAssistantView):
    """POST/GET /api/casasmart/pairing/codes: mint and list pairing codes."""

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
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error

        # "Add a device to <member>": the new device gets that member's current
        # role and rooms, whatever the payload says.
        role = payload.get("role", "")
        rooms = payload.get("rooms")
        member_id = payload.get("member_id")
        if member_id is not None:
            engine = get_engine(self._hass)
            if engine is None:
                return self.json_message(
                    "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
                )
            members = await self._hass.async_add_executor_job(engine.list_members)
            member = next((m for m in members if m["member_id"] == member_id), None)
            if member is None:
                return self.json_message("Unknown member", HTTPStatus.BAD_REQUEST)
            role = member["role"]
            rooms = member["rooms"]

        try:
            issued = await self._hass.async_add_executor_job(
                lambda: pairing.generate_code(
                    role=role,
                    rooms=rooms,
                    expires_in=payload.get("expires_in", "1d"),
                    member_id=member_id,
                )
            )
        except PairingError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        _LOGGER.info(
            "Pairing code minted by %s (role=%s%s)",
            claims["sub"],
            issued["role"],
            " add-device" if member_id else "",
        )
        # The v2 fields are added to the unchanged v1 response.
        return self.json(
            {**issued, **_payload_v2_fields(self._hass, issued["code"])},
            HTTPStatus.CREATED,
        )

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
    """DELETE /api/casasmart/pairing/codes/{code_id}: revoke a code."""

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
        revoked = await self._hass.async_add_executor_job(pairing.revoke_code, code_id)
        if not revoked:
            return self.json_message("Unknown pairing code", HTTPStatus.NOT_FOUND)
        return self.json({"revoked": code_id})


class CasaSmartUsersView(HomeAssistantView):
    """GET /api/casasmart/users: every paired device (admin only)."""

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
    """PATCH/DELETE /api/casasmart/users/{device_id}: edit or unpair a device.

    Either way the device's outstanding tokens stop working at once. The
    admin can't be edited or unpaired here; the owner's phone leaves only by
    unpairing itself, by owner recovery, or by a reset from Home Assistant.
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
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
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
                    rooms=payload.get("rooms", ...),
                )
            )
        except UnknownDeviceError:
            return self.json_message("Unknown device", HTTPStatus.NOT_FOUND)
        except UserManagementError as err:
            return self.json_message(str(err), HTTPStatus.FORBIDDEN)

        # Refresh the device's sensor.
        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})

        return self.json(updated)

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "users.manage")
        if error is not None:
            return error
        engine = get_engine(self._hass)
        if engine is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            member_id = await self._hass.async_add_executor_job(
                engine.delete_device, device_id
            )
        except UnknownDeviceError:
            return self.json_message("Unknown device", HTTPStatus.NOT_FOUND)
        except UserManagementError as err:
            return self.json_message(str(err), HTTPStatus.FORBIDDEN)

        push = get_push_store(self._hass)
        if push is not None:
            await self._hass.async_add_executor_job(push.unregister, device_id)

        # A member's favorites and settings go with their last device; a
        # member with another paired device keeps them.
        runtime = loaded_runtime_data(self._hass)
        if runtime is not None:

            def _prune_orphaned_member() -> None:
                if engine.member_device_count(member_id) != 0:
                    return
                runtime.registry.delete_favorites(member_id)
                runtime.user_settings.delete(member_id)

            await self._hass.async_add_executor_job(_prune_orphaned_member)

        # Remove the device's sensor.
        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})

        return self.json({"unpaired": device_id})
