"""CasaSmart water-tank REST endpoints.

Shelly provisioning, the reading ingest the Shelly posts to, and the app's
views, over TankEngine (tank.py). Endpoints under /api/casasmart:

- POST   /tank/provision                     set up a Shelly (registry.manage)
- POST   /tank/reading                       the Shelly's readings (device token)
- GET    /tank/devices                       every tank (devices.read)
- DELETE /tank/devices/{device_id}           remove a tank (registry.manage)
- GET    /tank/devices/{device_id}/readings  reading history (devices.read)
- PATCH  /tank/{device_id}/calibration       calibration (registry.manage)
- GET    /tank/{device_id}/status            live status (devices.read)

Provisioning uses the Shelly's Gen2 RPC API over plain HTTP, and only dials
private LAN addresses. Each reading fires EVENT_TANK_CHANGED.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import time
from http import HTTPStatus
from typing import Any

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_api import (
    authenticate_request,
    read_json_object,
    ready_or_503,
)
from .const import DOMAIN, EVENT_TANK_CHANGED
from .runtime_lookup import loaded_runtime_data
from .tank import (
    TANK_INGEST_URL_CONFIG_KEY,
    TANK_SCRIPT_NAME,
    DuplicateTankError,
    TankEngine,
    TankError,
    UnknownTankError,
    UnknownTokenError,
    build_tank_script,
    chunk_script_code,
)
from .throttle import FailureThrottle, ThrottledError

_LOGGER = logging.getLogger(__name__)


# Per-request budget for one call to the Shelly.
_SHELLY_RPC_TIMEOUT = aiohttp.ClientTimeout(total=8)

# After the upload, poll this long for a first reading to see whether the
# Shelly reaches the hub; none arriving is reported as verified: false.
_FIRST_READING_WAIT = 12.0
_FIRST_READING_POLL = 0.5

# Escalating lockouts for bad ingest tokens, keyed by client address.
_INGEST_THROTTLE = FailureThrottle("tank-ingest")

# Body fields PATCH .../calibration accepts; each is optional.
_CALIBRATION_FIELDS = (
    "calibration_voltage",
    "calibration_depth",
    "max_height",
    "low_percent",
)


class ShellyRpcError(Exception):
    """The device refused or broke the RPC conversation."""


# -- helpers ------------------------------------------------------------------


def get_tanks(hass: HomeAssistant) -> TankEngine | None:
    """The loaded entry's tank engine, or None when not set up."""
    runtime_data = loaded_runtime_data(hass)
    return runtime_data.tanks if runtime_data is not None else None


def _is_lan_target(ip: str) -> bool:
    """True for a private or link-local address the hub may dial.

    Provisioning sends requests to a caller-supplied address, so loopback, the
    public internet and the unspecified address (0.0.0.0 or ::, which reaches
    the hub itself) are refused.
    """
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        (parsed.is_private or parsed.is_link_local)
        and not parsed.is_loopback
        and not parsed.is_unspecified
    )


def _client_ip(request: web.Request) -> str:
    """Client address for throttling and logs.

    HA has already resolved request.remote from X-Forwarded-For for its
    trusted proxies, so behind the tunnel it is the real client. The raw
    headers aren't read: any peer can send them, and they would let a client
    pick a new throttle bucket for every request.
    """
    return request.remote or "unknown"


# -- Shelly Gen2 RPC ----------------------------------------------------------


async def _shelly_rpc(
    session: aiohttp.ClientSession,
    ip: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One Gen2 RPC call; raises ShellyRpcError on anything but success."""
    try:
        async with session.post(
            f"http://{ip}/rpc",
            json={"id": 1, "method": method, "params": params or {}},
            timeout=_SHELLY_RPC_TIMEOUT,
        ) as response:
            if response.status != HTTPStatus.OK:
                raise ShellyRpcError(f"{method}: HTTP {response.status}")
            body = await response.json(content_type=None)
    except ShellyRpcError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise ShellyRpcError(f"{method}: {err}") from err
    if not isinstance(body, dict):
        raise ShellyRpcError(f"{method}: malformed response")
    error = body.get("error")
    if error is not None:
        raise ShellyRpcError(f"{method}: {error}")
    result = body.get("result")
    return result if isinstance(result, dict) else {}


async def _fetch_device_info(session: aiohttp.ClientSession, ip: str) -> dict[str, Any]:
    """GET /shelly: the device's generation and id (no auth needed)."""
    try:
        async with session.get(
            f"http://{ip}/shelly", timeout=_SHELLY_RPC_TIMEOUT
        ) as response:
            if response.status != HTTPStatus.OK:
                raise ShellyRpcError(f"device info: HTTP {response.status}")
            info = await response.json(content_type=None)
    except ShellyRpcError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise ShellyRpcError(f"device info: {err}") from err
    if not isinstance(info, dict):
        raise ShellyRpcError("device info: malformed response")
    return info


async def _find_script_id(
    session: aiohttp.ClientSession, ip: str, name: str
) -> int | None:
    """The id of the Shelly script with this name, or None."""
    listing = await _shelly_rpc(session, ip, "Script.List")
    scripts = listing.get("scripts") or []
    if not isinstance(scripts, list):
        raise ShellyRpcError("Script.List: malformed response")
    for script in scripts:
        if isinstance(script, dict) and script.get("name") == name:
            script_id = script.get("id")
            return script_id if isinstance(script_id, int) else None
    return None


async def _remove_script(session: aiohttp.ClientSession, ip: str, name: str) -> bool:
    """Stop + delete the named script; False when it wasn't there."""
    script_id = await _find_script_id(session, ip, name)
    if script_id is None:
        return False
    try:
        await _shelly_rpc(session, ip, "Script.Stop", {"id": script_id})
    except ShellyRpcError:
        pass  # not running; the delete is what matters
    await _shelly_rpc(session, ip, "Script.Delete", {"id": script_id})
    return True


async def _push_script(session: aiohttp.ClientSession, ip: str, code: str) -> int:
    """Install and start the monitoring script and return its id.

    A script already called TANK_SCRIPT_NAME is removed first, so a retry
    replaces whatever an earlier attempt left.
    """
    await _remove_script(session, ip, TANK_SCRIPT_NAME)
    created = await _shelly_rpc(
        session, ip, "Script.Create", {"name": TANK_SCRIPT_NAME}
    )
    script_id = created.get("id")
    if not isinstance(script_id, int):
        raise ShellyRpcError("Script.Create returned no id")
    for index, chunk in enumerate(chunk_script_code(code)):
        await _shelly_rpc(
            session,
            ip,
            "Script.PutCode",
            {"id": script_id, "code": chunk, "append": index > 0},
        )
    # enable: start the script again after a device reboot.
    await _shelly_rpc(
        session,
        ip,
        "Script.SetConfig",
        {"id": script_id, "config": {"enable": True}},
    )
    await _shelly_rpc(session, ip, "Script.Start", {"id": script_id})
    return script_id


# -- views --------------------------------------------------------------------


class _TankView(HomeAssistantView):
    """Shared plumbing for the tank views."""

    requires_auth = False  # CasaSmart gates in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _tanks_or_503(self) -> tuple[TankEngine | None, web.Response | None]:
        """(engine, None), or (None, 503) while the hub is loading."""
        return ready_or_503(self, get_tanks(self._hass))

    def _ingest_url(self) -> str | None:
        """The tank_ingest_url override from hub config, or None.

        For setups where the hub's LAN address differs from what it sees
        itself (Docker Desktop, bridge networking).
        """
        runtime_data = loaded_runtime_data(self._hass)
        if runtime_data is not None:
            override = runtime_data.hub_config.get(TANK_INGEST_URL_CONFIG_KEY)
            if isinstance(override, str) and override.startswith(
                ("http://", "https://")
            ):
                return override.rstrip("/")
        return None

    async def _default_ingest_url(self) -> str | None:
        """The hub's LAN IP + HA's own HTTP port, or None if unknown.

        Plain HTTP, because the Shelly can't validate the hub's self-signed
        certificate. The request stays on the LAN, and its token can only
        record readings.
        """
        try:
            from homeassistant.components import network

            ip = await network.async_get_source_ip(self._hass, network.MDNS_TARGET_IP)
        except Exception as err:
            _LOGGER.warning("Tank ingest URL: source IP lookup failed: %s", err)
            return None
        if not ip:
            return None
        port = self._hass.http.server_port
        return f"http://{ip}:{port}/api/{DOMAIN}/tank/reading"


class CasaSmartTankProvisionView(_TankView):
    """POST /api/casasmart/tank/provision: set up a Shelly as a tank.

    Body: {"ip": "<LAN address>", "name"?}. The hub reads the Shelly's
    identity (Gen2+, device authentication off), mints the tank record and
    its token, uploads and starts the script, then waits briefly for a first
    reading. Answers 201 with the record, script_id, verified and
    first_reading; 409 if the tank is already registered; 502 if the Shelly
    can't be reached or the upload fails (the new record is deleted again, so
    a retry works). Needs registry.manage.
    """

    url = f"/api/{DOMAIN}/tank/provision"
    name = f"api:{DOMAIN}:tank:provision"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        ip = payload.get("ip")
        if not isinstance(ip, str) or not _is_lan_target(ip.strip()):
            return self.json_message("ip must be a LAN address", HTTPStatus.BAD_REQUEST)
        ip = ip.strip()

        ingest_url = self._ingest_url() or await self._default_ingest_url()
        if ingest_url is None:
            return self.json_message(
                "Hub LAN address unknown: set tank_ingest_url in hub config",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        try:
            info = await _fetch_device_info(session, ip)
        except ShellyRpcError as err:
            return self.json_message(
                f"Shelly unreachable: {err}", HTTPStatus.BAD_GATEWAY
            )
        device_id = info.get("id")
        generation = info.get("gen")
        if not isinstance(device_id, str) or not device_id:
            return self.json_message(
                "Device did not identify as a Shelly", HTTPStatus.BAD_REQUEST
            )
        if not isinstance(generation, int) or generation < 2:
            return self.json_message(
                "Only Gen2+ Shelly devices support hub provisioning",
                HTTPStatus.BAD_REQUEST,
            )
        if info.get("auth_en") is True:
            return self.json_message(
                "Shelly has device authentication enabled; disable it "
                "and provision again",
                HTTPStatus.BAD_REQUEST,
            )

        name = payload.get("name")
        try:
            record, token = await self._hass.async_add_executor_job(
                tanks.mint_device,
                device_id,
                name if isinstance(name, str) and name.strip() else "Water Tank",
                ip,
                info.get("model"),
            )
        except DuplicateTankError as err:
            return self.json_message(str(err), HTTPStatus.CONFLICT)
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        provisioned_at = time.time()
        try:
            script = build_tank_script(ingest_url, token)
            script_id = await _push_script(session, ip, script)
        except (ShellyRpcError, TankError) as err:
            _LOGGER.warning("Tank provision failed for %s: %s", ip, err)
            await self._undo_mint(tanks, record["device_id"], token)
            return self.json_message(
                f"Provisioning failed: {err}", HTTPStatus.BAD_GATEWAY
            )
        except Exception:
            # Unexpected: still undo the mint so a retry works, then re-raise.
            await self._undo_mint(tanks, record["device_id"], token)
            raise

        verified = False
        first_reading = None
        deadline = time.monotonic() + _FIRST_READING_WAIT
        while time.monotonic() < deadline:
            reading = await self._hass.async_add_executor_job(
                tanks.last_reading, record["device_id"]
            )
            if reading is not None and reading.get("t", 0) >= int(provisioned_at - 1):
                verified = True
                first_reading = reading
                break
            await asyncio.sleep(_FIRST_READING_POLL)

        _LOGGER.info(
            "Tank %s provisioned by %s (script %d, verified=%s)",
            record["device_id"],
            claims["sub"],
            script_id,
            verified,
        )
        return self.json(
            {
                **record,
                "script_id": script_id,
                "verified": verified,
                "first_reading": first_reading,
            },
            HTTPStatus.CREATED,
        )

    async def _undo_mint(self, tanks: TankEngine, device_id: str, token: str) -> None:
        """Delete the record this request minted, so a retry isn't a 409.

        A record minted since by another request (with a different token) is
        left alone.
        """
        try:
            await self._hass.async_add_executor_job(
                functools.partial(tanks.delete_device, device_id, token=token)
            )
        except UnknownTankError:
            pass  # already deleted, or re-minted by another request


class CasaSmartTankReadingView(_TankView):
    """POST /api/casasmart/tank/reading: readings from the Shelly script.

    Body: {"device_token": ..., "voltage": ...}. The device token is the
    credential, so any source is accepted: the Shelly may reach the hub on the
    LAN or through the Cloudflare tunnel. Bad tokens are throttled per client
    address.
    """

    url = f"/api/{DOMAIN}/tank/reading"
    name = f"api:{DOMAIN}:tank:reading"

    async def post(self, request: web.Request) -> web.Response:
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error

        source = _client_ip(request)
        try:
            _INGEST_THROTTLE.check(source)
        except ThrottledError as err:
            return web.json_response(
                {"message": str(err), "retry_after": int(err.retry_after)},
                status=HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(int(err.retry_after))},
            )

        try:
            device_id = await self._hass.async_add_executor_job(
                tanks.ingest, payload.get("device_token"), payload.get("voltage")
            )
        except UnknownTokenError:
            _INGEST_THROTTLE.record_failure(source)
            # The same answer whether the token is unknown or malformed.
            return self.json_message("Invalid device token", HTTPStatus.UNAUTHORIZED)
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        _INGEST_THROTTLE.clear(source)
        # Connected apps re-fetch the tank's level.
        self._hass.bus.async_fire(EVENT_TANK_CHANGED, {"device_id": device_id})
        return self.json({"ok": True, "device_id": device_id})


class CasaSmartTankDevicesView(_TankView):
    """GET /api/casasmart/tank/devices: every tank with its last reading."""

    url = f"/api/{DOMAIN}/tank/devices"
    name = f"api:{DOMAIN}:tank:devices"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        devices = await self._hass.async_add_executor_job(tanks.list_devices)
        return self.json({"devices": devices})


class CasaSmartTankDeviceView(_TankView):
    """DELETE /api/casasmart/tank/devices/{device_id}: remove a tank.

    Removes the script from the Shelly if it can be reached (an unplugged
    device doesn't block the delete), then deletes the record and readings.
    Needs registry.manage.
    """

    url = f"/api/{DOMAIN}/tank/devices/{{device_id}}"
    name = f"api:{DOMAIN}:tank:device"

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready

        try:
            record = await self._hass.async_add_executor_job(
                tanks.get_device, device_id
            )
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)

        ip = record.get("ip")
        if isinstance(ip, str) and _is_lan_target(ip):
            from homeassistant.helpers.aiohttp_client import (
                async_get_clientsession,
            )

            try:
                await _remove_script(
                    async_get_clientsession(self._hass), ip, TANK_SCRIPT_NAME
                )
            except ShellyRpcError as err:
                _LOGGER.info("Tank %s: script cleanup skipped (%s)", device_id, err)

        try:
            await self._hass.async_add_executor_job(tanks.delete_device, device_id)
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        return self.json({"deleted": device_id})


class CasaSmartTankReadingsView(_TankView):
    """GET /api/casasmart/tank/devices/{device_id}/readings?days=N.

    Readings newest first as {"t": unix_seconds, "v": voltage, "p": percent},
    for the last 7 days by default. p is null for an uncalibrated tank. Needs
    devices.read.
    """

    url = f"/api/{DOMAIN}/tank/devices/{{device_id}}/readings"
    name = f"api:{DOMAIN}:tank:readings"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        raw_days = request.query.get("days", "7")
        try:
            days = int(raw_days)
        except ValueError:
            return self.json_message(
                f"Invalid days: {raw_days!r}", HTTPStatus.BAD_REQUEST
            )
        try:
            readings = await self._hass.async_add_executor_job(
                tanks.recent_readings, device_id, days
            )
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json({"device_id": device_id, "readings": readings})


class CasaSmartTankCalibrationView(_TankView):
    """PATCH /api/casasmart/tank/{device_id}/calibration.

    Body: any of calibration_voltage, calibration_depth, max_height and
    low_percent; omitted fields are unchanged. low_percent must be 1-30 and
    the others positive numbers. Needs registry.manage.
    """

    url = f"/api/{DOMAIN}/tank/{{device_id}}/calibration"
    name = f"api:{DOMAIN}:tank:calibration"

    async def patch(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        payload, error = await read_json_object(self, request)
        if error is not None:
            return error
        kwargs = {key: payload[key] for key in _CALIBRATION_FIELDS if key in payload}
        if not kwargs:
            return self.json_message(
                "No calibration fields provided", HTTPStatus.BAD_REQUEST
            )
        try:
            record = await self._hass.async_add_executor_job(
                functools.partial(tanks.set_calibration, device_id, **kwargs)
            )
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(record)


class CasaSmartTankStatusView(_TankView):
    """GET /api/casasmart/tank/{device_id}/status: the computed live status.

    See TankEngine.status. Needs devices.read.
    """

    url = f"/api/{DOMAIN}/tank/{{device_id}}/status"
    name = f"api:{DOMAIN}:tank:status"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        try:
            status = await self._hass.async_add_executor_job(tanks.status, device_id)
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        return self.json(status)
