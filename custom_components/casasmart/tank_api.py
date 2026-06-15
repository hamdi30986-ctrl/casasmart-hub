"""Shelly tank endpoints + Gen2 provisioner (mini-block MB-1).

The hub half of the post-funeral tank story:

- ``POST /api/casasmart/tank/provision`` — the hub WRITES the monitoring
  script to the Shelly itself over Gen2 scripting RPC
  (``Script.Create`` / ``Script.PutCode`` / ``Script.Start``) with the
  hub's LAN ingest URL + a freshly minted device token baked in. No
  hand-edited scripts, no hardcoded IPs in the app — re-provisioning
  after a hub IP change or token rotation is the same one call
  (``registry.manage``).
- ``POST /api/casasmart/tank/reading`` — the ingest endpoint the script
  POSTs to every 5 minutes. The minted device token IS the credential
  (same posture as the old Supabase ``shelly_push`` shape): no JWT, but
  LAN-only like enroll/recover (the Shelly lives on the LAN; a leaked
  token is useless remotely) and throttled per source on bad tokens.
- ``GET /api/casasmart/tank/devices`` + ``GET .../{id}/readings`` — what
  the app reads instead of its phone-local log (``devices.read``).
- ``DELETE /api/casasmart/tank/devices/{id}`` — drop the tank; the
  script is removed from the device best-effort first
  (``registry.manage``).

The Shelly RPC leg rides HA's shared aiohttp session. The target IP is
validated to be a private/LAN address before the hub dials it — the
provision endpoint must not be usable as an SSRF proxy.
"""

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
    get_extra_lan_cidrs,
    is_lan_request,
    json_body,
)
from .const import DOMAIN
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

# Per-RPC budget against the Shelly — LAN round-trips, generous for a
# busy device but far under anything the app would sit through.
_SHELLY_RPC_TIMEOUT = aiohttp.ClientTimeout(total=8)
# How long the provision call waits for the freshly started script's
# first POST to land before reporting verified=false (the script pushes
# once immediately on start, so success lands in low single seconds).
_FIRST_READING_WAIT = 12.0
_FIRST_READING_POLL = 0.5

# Module-level so both listeners (HA port + TLS port) share one wall.
_INGEST_THROTTLE = FailureThrottle("tank-ingest")


class ShellyRpcError(Exception):
    """The device refused or broke the RPC conversation."""


def get_tanks(hass: HomeAssistant) -> TankEngine | None:
    """The loaded entry's tank engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.tanks


def _is_lan_target(ip: str) -> bool:
    """True when ``ip`` is a private LAN address the hub may dial.

    The provisioner makes server-side HTTP requests to a caller-supplied
    address — restrict it to RFC1918/link-local space so the endpoint
    can't be aimed at loopback services or the public internet.
    """
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


async def _fetch_device_info(
    session: aiohttp.ClientSession, ip: str
) -> dict[str, Any]:
    """``GET /shelly`` — generation + stable device id, pre-credentials."""
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
    """Stop + delete the named script; False when it wasn't there."""
    script_id = await _find_script_id(session, ip, name)
    if script_id is None:
        return False
    try:
        await _shelly_rpc(session, ip, "Script.Stop", {"id": script_id})
    except ShellyRpcError:
        pass  # already stopped is fine — delete is what matters
    await _shelly_rpc(session, ip, "Script.Delete", {"id": script_id})
    return True


async def _push_script(
    session: aiohttp.ClientSession, ip: str, code: str
) -> int:
    """Create/replace + upload + autostart + start the monitoring script."""
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
    # enable=true -> the script survives a device reboot (autostart).
    await _shelly_rpc(
        session,
        ip,
        "Script.SetConfig",
        {"id": script_id, "config": {"enable": True}},
    )
    await _shelly_rpc(session, ip, "Script.Start", {"id": script_id})
    return script_id


class _TankView(HomeAssistantView):
    """Shared plumbing for the tank views."""

    requires_auth = False  # CasaSmart gates in-handler

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
        """Where provisioned Shellys POST readings.

        ``tank_ingest_url`` in hub config overrides (deployments whose
        LAN-visible address differs from the hub's own view of it — the
        dev rig's Docker port proxy); the default is the hub's LAN IP +
        HA's own HTTP port. Plain HTTP by design: the Gen2 HTTP client
        can't validate the hub's self-signed LAN cert, the POST never
        leaves the LAN, and the token it carries can do exactly one
        thing — record a tank reading.
        """
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
        except Exception as err:  # noqa: BLE001 — degrade, never crash provision
            _LOGGER.warning("Tank ingest URL: source IP lookup failed: %s", err)
            return None
        if not ip:
            return None
        port = self._hass.http.server_port
        return f"http://{ip}:{port}/api/{DOMAIN}/tank/reading"


class CasaSmartTankProvisionView(_TankView):
    """POST /api/casasmart/tank/provision — hub writes the Shelly script.

    Body: ``{"ip": "<lan-ip>", "name": "<tank name>"}``. The hub reads
    the device's identity, mints the ingest token, pushes the script and
    waits briefly for the first reading to land — the response carries
    ``verified`` + ``first_reading`` so the app can confirm end-to-end
    without polling.
    """

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
            # Device-local auth would need credentials we don't manage;
            # fail with a actionable message instead of a cryptic 401.
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
            # The record (and its fresh token) stays — provisioning again
            # is the retry path and replaces both cleanly.
            _LOGGER.warning("Tank provision failed for %s: %s", ip, err)
            return self.json_message(
                f"Provisioning failed: {err}", HTTPStatus.BAD_GATEWAY
            )

        # The script pushes once on start — wait briefly so the app gets
        # an end-to-end verdict in the same round-trip.
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


class CasaSmartTankReadingView(_TankView):
    """POST /api/casasmart/tank/reading — the Shelly script's ingest.

    The minted device token is the credential (``{"device_token": ...,
    "voltage": ...}``). LAN-only like enroll — the device lives on the
    LAN and a leaked token must be useless remotely. Bad tokens are
    throttled per source with the shared escalating walls.
    """

    url = f"/api/{DOMAIN}/tank/reading"
    name = f"api:{DOMAIN}:tank:reading"

    async def post(self, request: web.Request) -> web.Response:
        tanks, not_ready = self._tanks_or_503()
        if not_ready is not None:
            return not_ready
        if not is_lan_request(request, get_extra_lan_cidrs(self._hass)):
            _LOGGER.warning(
                "Tank reading refused (non-LAN source: %s)", request.remote
            )
            return self.json_message(
                "Tank ingest is only available on the hub's own network",
                HTTPStatus.FORBIDDEN,
            )
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        source = request.remote or "unknown"
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
            # One generic bucket — no hint whether the token is unknown,
            # rotated or malformed.
            return self.json_message(
                "Invalid device token", HTTPStatus.UNAUTHORIZED
            )
        except TankError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        _INGEST_THROTTLE.clear(source)
        return self.json({"ok": True, "device_id": device_id})


class CasaSmartTankDevicesView(_TankView):
    """GET /api/casasmart/tank/devices — every tank with its last reading."""

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
    """DELETE /api/casasmart/tank/devices/{device_id} — remove a tank.

    Best-effort removes the monitoring script from the Shelly first (an
    unplugged device must not block the delete), then drops the record —
    the token dies with it.
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
    """GET /api/casasmart/tank/devices/{device_id}/readings?days=N.

    The 24/7 history the phone-local log can't have — newest first,
    ``{"t": unix_seconds, "v": voltage, "p": percent}``. The hub computes
    ``p`` from the device's calibration (B8 Piece 4b — the app no longer does
    the math; ``p`` is null for an uncalibrated tank).
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


# Fields the calibration PATCH accepts — anything else in the body is ignored,
# so a future app can add keys without this endpoint 400-ing old hubs.
_CALIBRATION_FIELDS = (
    "calibration_voltage",
    "calibration_depth",
    "max_height",
    "low_percent",
)


class CasaSmartTankCalibrationView(_TankView):
    """PATCH /api/casasmart/tank/{device_id}/calibration.

    The hub owns the voltage→percent calibration and the low-water threshold
    (B8 Piece 4b — the app is pure UI). Body carries any subset of
    ``{"calibration_voltage", "calibration_depth", "max_height",
    "low_percent"}``; omitted fields are left unchanged, so the calibration
    dialog (the three calibration inputs) and the notification slider
    (``low_percent`` alone) share one endpoint. ``low_percent`` is validated to
    the 1-30 range and the rest to be positive numbers — the engine is the one
    that says no. ``registry.manage`` gated, like provision/delete.
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
    """GET /api/casasmart/tank/{device_id}/status.

    The computed live status the app displays instead of doing the math
    itself (B8 Piece 4b): ``{voltage, percent, low_percent, is_low,
    last_reading}``. ``voltage``/``percent`` are null with no reading yet or an
    uncalibrated tank. ``devices.read`` gated, like the device/readings GETs.
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
            status = await self._hass.async_add_executor_job(
                tanks.status, device_id
            )
        except UnknownTankError:
            return self.json_message("Unknown tank device", HTTPStatus.NOT_FOUND)
        return self.json(status)
