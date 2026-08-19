"""CasaSmart runtime component."""

from __future__ import annotations

import ipaddress
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
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
from .throttle import ThrottledError
from .tunnel import TUNNEL_URL_CONFIG_KEY, normalize_tunnel_url

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


def _get_push_dispatcher(hass: HomeAssistant):
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.push_dispatcher


def get_engine(hass: HomeAssistant) -> AuthEngine | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.auth


def get_pairing(hass: HomeAssistant) -> PairingManager | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.pairing


def get_recovery(hass: HomeAssistant) -> RecoveryManager | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.recovery


def get_provision_secret(hass: HomeAssistant) -> str | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.hub_config.get(PROVISION_SECRET_CONFIG_KEY)


def notify_recovery_code(hass: HomeAssistant, code: str) -> None:
    """CasaSmart runtime component."""
    persistent_notification.async_create(
        hass,
        f"Owner recovery code: **{code}**\n\n"
        "Engrave this on the recovery card and store it with the owner. "
        "It is permanent and reusable (LAN-only) — redeeming it re-installs "
        "the owner's phone as admin, the same card keeps working, and it "
        "survives factory reset.",
        title="CasaSmart Hub — recovery code",
        notification_id=f"{DOMAIN}_recovery_code",
    )


def arm_recovery(hass: HomeAssistant) -> None:
    """CasaSmart runtime component."""
    recovery = get_recovery(hass)
    if recovery is None:
        return
    code = recovery.ensure_armed()
    if code is not None:
        hass.loop.call_soon_threadsafe(notify_recovery_code, hass, code)


def is_lan_request(
    request: web.Request, extra_cidrs: list[str] | None = None
) -> bool:
    """CasaSmart runtime component."""
    try:
        remote = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    if (remote.is_private or remote.is_link_local) and not remote.is_loopback:
        return True
    for cidr in extra_cidrs or []:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            _LOGGER.error("Ignoring invalid pairing_extra_lan_cidrs entry %r", cidr)
            continue
        if not network.is_private:



            _LOGGER.error(
                "Refusing PUBLIC pairing_extra_lan_cidrs entry %r — "
                "LAN-widening is private-only",
                cidr,
            )
            continue
        if remote in network:
            return True
    return False


def get_extra_lan_cidrs(hass: HomeAssistant) -> list[str]:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return []
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    cidrs = runtime_data.hub_config.get("pairing_extra_lan_cidrs")
    if not isinstance(cidrs, list):
        return []
    return [cidr for cidr in cidrs if isinstance(cidr, str)]


def is_remote_pairing_enabled(hass: HomeAssistant) -> bool:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return False
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.hub_config.get(REMOTE_PAIRING_ENABLED_CONFIG_KEY) is True


def authenticate_request(
    hass: HomeAssistant, request: web.Request, permission: str
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class CasaSmartEnrollView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/enroll"
    name = f"api:{DOMAIN}:auth:enroll"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _sticker_hash(self) -> str | None:
        """CasaSmart runtime component."""
        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return None
        stored = entries[0].runtime_data.hub_config.get(
            BOOTSTRAP_CODE_HASH_CONFIG_KEY
        )
        return stored if isinstance(stored, str) and stored else None

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        pairing = get_pairing(self._hass)
        if engine is None or pairing is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        lan_source = is_lan_request(request, get_extra_lan_cidrs(self._hass))
        if not lan_source and not is_remote_pairing_enabled(self._hass):





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















        existing = await self._hass.async_add_executor_job(
            engine.device_for_public_key, payload.get("public_key", "")
        )
        if existing is not None:


            own_code_hash = existing.pop("enrolled_code_hash", None)
            allowed = tuple(
                h for h in (self._sticker_hash(), own_code_hash) if h
            )
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


                return self.json_message(
                    "Invalid pairing code", HTTPStatus.UNAUTHORIZED
                )
            return self.json(existing, HTTPStatus.CREATED)

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



            _LOGGER.warning(
                "Pairing refused: LAN-only code class from remote source %s",
                request.remote,
            )
            return self.json_message(
                "Pairing is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        except HubAlreadyClaimedError:


            return self.json_message(
                "This hub is already paired", HTTPStatus.CONFLICT
            )
        except CodeInvalidError:

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
                    enrolled_via=grant.get("code_id"),
                    member_id=grant.get("member_id"),
                    code_hash=hash_code(payload.get("pairing_code", "")),
                )
            )
        except EnrollError as err:


            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        if grant["role"] == ROLE_ADMIN:


            await self._hass.async_add_executor_job(arm_recovery, self._hass)


        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})








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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/recover"
    name = f"api:{DOMAIN}:auth:recover"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        engine = get_engine(self._hass)
        recovery = get_recovery(self._hass)
        if engine is None or recovery is None:
            return self.json_message("Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE)
        if not is_lan_request(request, get_extra_lan_cidrs(self._hass)):

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



            await self._hass.async_add_executor_job(arm_recovery, self._hass)
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)


        await self._hass.async_add_executor_job(arm_recovery, self._hass)


        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})

        return self.json(
            {"device_id": device_id, "role": "admin", "rooms": None},
            HTTPStatus.CREATED,
        )






















PAIRING_PAYLOAD_VERSION = 2


_DEEP_LINK_BASE = "casasmart://family"


def _get_loaded_entry(hass: HomeAssistant):
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _payload_v2_fields(hass: HomeAssistant, code: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    fields: dict[str, Any] = {"payload_version": PAIRING_PAYLOAD_VERSION}



    params = [("code", code), ("v", str(PAIRING_PAYLOAD_VERSION))]

    entry = _get_loaded_entry(hass)
    if entry is not None:
        runtime_data = entry.runtime_data
        tls = getattr(runtime_data, "tls", None)
        if tls is not None:
            fingerprint = tls.material.identity_fingerprint
            fields["identity_fingerprint"] = fingerprint
            params.append(("fp", fingerprint))







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


class CasaSmartPairingCodesView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/pairing/codes"
    name = f"api:{DOMAIN}:pairing:codes"
    requires_auth = False

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
            member = next(
                (m for m in members if m["member_id"] == member_id), None
            )
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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/pairing/codes/{{code_id}}"
    name = f"api:{DOMAIN}:pairing:code"
    requires_auth = False

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/users"
    name = f"api:{DOMAIN}:users"
    requires_auth = False

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/users/{{device_id}}"
    name = f"api:{DOMAIN}:user"
    requires_auth = False

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


        push = _get_push_store(self._hass)
        if push is not None:
            await self._hass.async_add_executor_job(
                push.unregister, device_id
            )






        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        runtime = entries[0].runtime_data if entries else None
        if runtime is not None:

            def _prune_orphaned_member() -> None:
                if engine.member_device_count(member_id) != 0:
                    return
                runtime.registry.delete_favorites(member_id)
                runtime.user_settings.delete(member_id)

            await self._hass.async_add_executor_job(_prune_orphaned_member)


        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})

        return self.json({"unpaired": device_id})


class CasaSmartUnpairSelfView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/unpair-self"
    name = f"api:{DOMAIN}:auth:unpair-self"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:


        claims, error = authenticate_request(self._hass, request, "devices.read")
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


            return self.json({"unpaired": device_id, "hub_unclaimed": False})

        push = _get_push_store(self._hass)
        if push is not None:
            await self._hass.async_add_executor_job(push.unregister, device_id)

        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        runtime = entries[0].runtime_data if entries else None
        unclaimed = False
        if runtime is not None:

            def _finish_leave() -> bool:


                if engine.member_device_count(member_id) == 0:
                    runtime.registry.delete_favorites(member_id)
                    runtime.user_settings.delete(member_id)
                if engine.has_admin():
                    return False


                code_hash = runtime.hub_config.get(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
                if not code_hash:



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


class CasaSmartChallengeView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/challenge"
    name = f"api:{DOMAIN}:auth:challenge"
    requires_auth = False

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/token"
    name = f"api:{DOMAIN}:auth:token"
    requires_auth = False

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

            return self.json_message(str(err), HTTPStatus.UNAUTHORIZED)

        return self.json(issued)


class CasaSmartWidgetTokenView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/widget-token"
    name = f"api:{DOMAIN}:auth:widget-token"
    requires_auth = False

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


            return self.json_message(
                "Invalid or expired token", HTTPStatus.UNAUTHORIZED
            )

        _LOGGER.info("Widget token minted for %s", claims["sub"])
        return self.json(issued, HTTPStatus.CREATED)


class CasaSmartWhoamiView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/auth/whoami"
    name = f"api:{DOMAIN}:auth:whoami"
    requires_auth = False

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


def _throttled_response(err: ThrottledError) -> web.Response:
    return web.json_response(
        {"message": str(err), "retry_after": int(err.retry_after)},
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(int(err.retry_after))},
    )
