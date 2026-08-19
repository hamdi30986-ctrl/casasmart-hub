"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .auth_api import (
    authenticate_request,
    json_body,
)
from .const import DOMAIN, EVENT_TANK_CHANGED
from .tank import (
    TANK_INGEST_URL_CONFIG_KEY,
    TANK_SCRIPT_NAME,
    TankEngine,
    TankError,
    UnknownTankError,
    UnknownTokenError,
    build_tank_script,
    chunk_script_code,
)
from .throttle import FailureThrottle, ThrottledError

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)



_SHELLY_RPC_TIMEOUT = aiohttp.ClientTimeout(total=8)



_FIRST_READING_WAIT = 12.0
_FIRST_READING_POLL = 0.5


_INGEST_THROTTLE = FailureThrottle("tank-ingest")


class ShellyRpcError(Exception):
    """CasaSmart runtime component."""


def get_tanks(hass: HomeAssistant) -> TankEngine | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.tanks


def _is_lan_target(ip: str) -> bool:
    """CasaSmart runtime component."""
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (parsed.is_private or parsed.is_link_local) and not parsed.is_loopback


async def _shelly_rpc(
    session: aiohttp.ClientSession,
    ip: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CasaSmart runtime component."""
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


async def _fetch_device_info(
    session: aiohttp.ClientSession, ip: str
) -> dict[str, Any]:
    """CasaSmart runtime component."""
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
    listing = await _shelly_rpc(session, ip, "Script.List")
    for script in listing.get("scripts") or []:
        if isinstance(script, dict) and script.get("name") == name:
            script_id = script.get("id")
            return script_id if isinstance(script_id, int) else None
    return None


async def _remove_script(
    session: aiohttp.ClientSession, ip: str, name: str
) -> bool:
    """CasaSmart runtime component."""
    script_id = await _find_script_id(session, ip, name)
    if script_id is None:
        return False
    try:
        await _shelly_rpc(session, ip, "Script.Stop", {"id": script_id})
    except ShellyRpcError:
        pass
    await _shelly_rpc(session, ip, "Script.Delete", {"id": script_id})
    return True


async def _push_script(
    session: aiohttp.ClientSession, ip: str, code: str
) -> int:
    """CasaSmart runtime component."""
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

    await _shelly_rpc(
        session,
        ip,
        "Script.SetConfig",
        {"id": script_id, "config": {"enable": True}},
    )
    await _shelly_rpc(session, ip, "Script.Start", {"id": script_id})
    return script_id


class _TankView(HomeAssistantView):
    """CasaSmart runtime component."""

    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _tanks_or_503(self) -> tuple[TankEngine | None, web.Response | None]:
        tanks = get_tanks(self._hass)
        if tanks is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return tanks, None

    def _ingest_url(self) -> str | None:
        """CasaSmart runtime component."""
        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        if entries:
            runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
            override = runtime_data.hub_config.get(TANK_INGEST_URL_CONFIG_KEY)
            if isinstance(override, str) and override.startswith(
                ("http://", "https://")
            ):
                return override.rstrip("/")
        return None

    async def _default_ingest_url(self) -> str | None:
        try:
            from homeassistant.components import network

            ip = await network.async_get_source_ip(
                self._hass, network.MDNS_TARGET_IP
            )
        except Exception as err:
            _LOGGER.warning("Tank ingest URL: source IP lookup failed: %s", err)
            return None
        if not ip:
            return None
        port = self._hass.http.server_port
        return f"http://{ip}:{port}/api/{DOMAIN}/tank/reading"


class CasaSmartTankProvisionView(_TankView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/tank/provision"
    name = f"api:{DOMAIN}:tank:provision"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(
            self._hass, request, "registry.manage"
        )
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        ip = payload.get("ip")
        if not isinstance(ip, str) or not _is_lan_target(ip.strip()):
            return self.json_message(
                "ip must be a LAN address", HTTPStatus.BAD_REQUEST
            )
        ip = ip.strip()

        ingest_url = self._ingest_url() or await self._default_ingest_url()
        if ingest_url is None:
            return self.json_message(
                "Hub LAN address unknown — set tank_ingest_url in hub config",
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
                "Shelly has device authentication enabled — disable it "
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
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        provisioned_at = time.time()
        try:
            script = build_tank_script(ingest_url, token)
            script_id = await _push_script(session, ip, script)
        except (ShellyRpcError, TankError) as err:


            _LOGGER.warning("Tank provision failed for %s: %s", ip, err)
            return self.json_message(
                f"Provisioning failed: {err}", HTTPStatus.BAD_GATEWAY
            )



        verified = False
        first_reading = None
        deadline = time.monotonic() + _FIRST_READING_WAIT
        while time.monotonic() < deadline:
            reading = await self._hass.async_add_executor_job(
                tanks.last_reading, record["device_id"]
            )
            if reading is not None and reading.get("t", 0) >= int(
                provisioned_at - 1
            ):
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


def _client_ip(request: web.Request) -> str:
    """CasaSmart runtime component."""
    for header in ("CF-Connecting-IP", "X-Forwarded-For"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.remote or "unknown"


class CasaSmartTankReadingView(_TankView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/tank/reading"
    name = f"api:{DOMAIN}:tank:reading"

    async def post(self, request: web.Request) -> web.Response:
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )




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


            return self.json_message(
                "Invalid device token", HTTPStatus.UNAUTHORIZED
            )
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        _INGEST_THROTTLE.clear(source)


        self._hass.bus.async_fire(EVENT_TANK_CHANGED, {"device_id": device_id})
        return self.json({"ok": True, "device_id": device_id})


class CasaSmartTankDevicesView(_TankView):
    """CasaSmart runtime component."""

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
    """CasaSmart runtime component."""

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
                _LOGGER.info(
                    "Tank %s: script cleanup skipped (%s)", device_id, err
                )

        try:
            await self._hass.async_add_executor_job(
                tanks.delete_device, device_id
            )
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        return self.json({"deleted": device_id})


class CasaSmartTankReadingsView(_TankView):
    """CasaSmart runtime component."""

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




_CALIBRATION_FIELDS = (
    "calibration_voltage",
    "calibration_depth",
    "max_height",
    "low_percent",
)


class CasaSmartTankCalibrationView(_TankView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/tank/{{device_id}}/calibration"
    name = f"api:{DOMAIN}:tank:calibration"

    async def patch(self, request: web.Request, device_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "registry.manage")
        if error is not None:
            return error
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
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
    """CasaSmart runtime component."""

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
            status = await self._hass.async_add_executor_job(
                tanks.status, device_id
            )
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        return self.json(status)
