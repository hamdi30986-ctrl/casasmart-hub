"""CasaSmart REST API (Track B — B1.3 skeleton + B1.4 entity bridge).

Unauthenticated endpoints (the only two, both read-only):

- ``GET /api/casasmart/handshake`` — the version contract between app and
  hub. The app sends ``X-CasaSmart-API-Version`` and decides from the
  response whether it can talk to this hub (plan: "API Version Handshake").
  No auth: the app must read this BEFORE it has any credentials, and the
  response contains nothing sensitive (versions only).
- ``GET /api/casasmart/health`` — liveness probe for external monitoring
  (watchdog, restore LXC, uptime checks). Reports integration + storage
  state. No auth for the same reason a load-balancer health check has none.

Entity bridge (B1.4) — authenticated:

- ``GET /api/casasmart/devices`` — curated device list (exposed domains
  only, allowlisted attributes, area names resolved).
- ``GET /api/casasmart/devices/{entity_id}`` — one device.
- ``POST /api/casasmart/devices/{entity_id}/command`` — whitelisted
  control: ``{"action": "turn_on", "data": {...}}`` validated against
  the per-domain command table before any service call.

Auth (B1.6): the device views are gated by the CasaSmart auth engine —
``auth_api.authenticate_request`` validates the hub-issued JWT and checks
the endpoint's permission; HA bearer tokens no longer grant access here.
Room-scoped tokens additionally pass every device through
``filtering.in_scope`` so a scoped user can't see or touch other rooms.
The auth endpoints themselves (enroll / challenge / token) live in
``auth_api.py``.

WebSocket (B1.5): ``/api/casasmart/ws`` is registered here too but lives
in ``ws.py`` (first-frame auth, real-time push through the same
``filtering`` code path as the views above).
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    API_VERSION,
    API_VERSION_HEADER,
    DOMAIN,
    MIN_APP_VERSION,
    SUPPORTED_API_VERSIONS,
)
from .auth_api import (
    CasaSmartChallengeView,
    CasaSmartEnrollView,
    CasaSmartPairingCodesView,
    CasaSmartPairingCodeView,
    CasaSmartRecoverView,
    CasaSmartTokenView,
    CasaSmartUsersView,
    CasaSmartUserView,
    authenticate_request,
)
from .entity_bridge import CommandError, validate_command
from .filtering import in_scope, is_served, serialize_device
from .tunnel import TUNNEL_URL_CONFIG_KEY, normalize_tunnel_url
from .registry_api import (
    CasaSmartDeviceAssignmentView,
    CasaSmartFavoritesView,
    CasaSmartFloorsView,
    CasaSmartFloorView,
    CasaSmartRegistryView,
    CasaSmartRoomsView,
    CasaSmartRoomView,
    CasaSmartSceneActivateView,
    CasaSmartScenesView,
    CasaSmartSceneView,
)
from .ws import CasaSmartWebSocketView

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def build_views(hass: HomeAssistant, hub_version: str) -> list[HomeAssistantView]:
    """Fresh instances of every CasaSmart view — THE canonical endpoint list.

    Both listeners consume this: the plain views on HA's own port and the
    B10 TLS listener on the hub's dedicated HTTPS port. One list means the
    two surfaces can never drift (same endpoints, same auth gates, same
    filtering).
    """
    return [
        CasaSmartHandshakeView(hass, hub_version),
        CasaSmartHealthView(hass, hub_version),
        CasaSmartDevicesView(hass),
        CasaSmartDeviceView(hass),
        CasaSmartCommandView(hass),
        CasaSmartWebSocketView(hass, hub_version),
        CasaSmartEnrollView(hass),
        CasaSmartRecoverView(hass),
        CasaSmartChallengeView(hass),
        CasaSmartTokenView(hass),
        CasaSmartPairingCodesView(hass),
        CasaSmartPairingCodeView(hass),
        CasaSmartUsersView(hass),
        CasaSmartUserView(hass),
        CasaSmartRegistryView(hass),
        CasaSmartFloorsView(hass),
        CasaSmartFloorView(hass),
        CasaSmartRoomsView(hass),
        CasaSmartRoomView(hass),
        CasaSmartDeviceAssignmentView(hass),
        CasaSmartScenesView(hass),
        CasaSmartSceneView(hass),
        CasaSmartSceneActivateView(hass),
        CasaSmartFavoritesView(hass),
    ]


def async_register_views(hass: HomeAssistant, hub_version: str) -> None:
    """Register the CasaSmart REST views (idempotent across entry reloads).

    HA's router can't unregister views, so a config-entry reload would
    register duplicates — guard with a domain-scoped flag.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("views_registered"):
        return
    for view in build_views(hass, hub_version):
        hass.http.register_view(view)
    domain_data["views_registered"] = True
    _LOGGER.debug("CasaSmart REST + WS views registered")


def _get_runtime_data(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    """Return the loaded entry's runtime data, or None if not loaded."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    return entries[0].runtime_data


class CasaSmartHandshakeView(HomeAssistantView):
    """GET /api/casasmart/handshake — the app↔hub version contract.

    B10 additions: the hub's permanent TLS identity public key (+ SPKI
    SHA-256 fingerprint) and the HTTPS port. The app reads these AT
    PAIRING (trust-on-first-use, on the LAN) and pins the identity key —
    every later TLS connection must present a cert signed by it. Public
    keys are public; serving them unauthenticated leaks nothing.
    """

    url = f"/api/{DOMAIN}/handshake"
    name = f"api:{DOMAIN}:handshake"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version
        # The handshake doubles as the app's reachability probe, so a
        # misconfigured tunnel_url would otherwise warn on every probe.
        self._tunnel_warned = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the version contract; echo compatibility if app sent its version."""
        body: dict[str, Any] = {
            "api_version": API_VERSION,
            "min_app_version": MIN_APP_VERSION,
            "hub_version": self._hub_version,
            "supported_api_versions": list(SUPPORTED_API_VERSIONS),
        }

        runtime_data = _get_runtime_data(self._hass)
        if runtime_data is not None and runtime_data.tls is not None:
            body["tls"] = {
                "port": runtime_data.tls.port,
                "identity_key": runtime_data.tls.material.identity_public_pem,
                "identity_fingerprint_sha256": (
                    runtime_data.tls.material.identity_fingerprint
                ),
            }

        # B7: advertise the installer-configured remote path so the app
        # captures it AT PAIRING (like the TLS pin above). Absent when not
        # configured or unusable — a bad URL degrades to LAN-only, it is
        # never "fixed up" and handed to phones.
        if runtime_data is not None:
            raw_tunnel = runtime_data.hub_config.get(TUNNEL_URL_CONFIG_KEY)
            tunnel_url = normalize_tunnel_url(raw_tunnel)
            if tunnel_url is not None:
                body["tunnel"] = {"url": tunnel_url}
            elif raw_tunnel is not None and not self._tunnel_warned:
                self._tunnel_warned = True
                _LOGGER.warning(
                    "Configured %s is not a usable https URL — "
                    "tunnel not advertised: %r",
                    TUNNEL_URL_CONFIG_KEY,
                    raw_tunnel,
                )

        # Additive convenience: if the app declared its API version, tell it
        # outright whether we speak it. Malformed header -> 400, not a guess.
        app_api_version = request.headers.get(API_VERSION_HEADER)
        if app_api_version is not None:
            try:
                requested = int(app_api_version)
            except ValueError:
                return self.json_message(
                    f"Invalid {API_VERSION_HEADER} header: {app_api_version!r}",
                    HTTPStatus.BAD_REQUEST,
                )
            body["compatible"] = requested in SUPPORTED_API_VERSIONS

        return self.json(body)


class CasaSmartHealthView(HomeAssistantView):
    """GET /api/casasmart/health — liveness probe for external monitoring."""

    url = f"/api/{DOMAIN}/health"
    name = f"api:{DOMAIN}:health"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.Response:
        """Report integration + storage health.

        200 with status "ok" only when the entry is loaded AND the storage
        layer answers a real read (schema_version hits SQLite). Anything
        else is 503 so dumb HTTP monitors can alert on status code alone.
        """
        body: dict[str, Any] = {
            "status": "ok",
            "hub_version": self._hub_version,
            "api_version": API_VERSION,
        }

        runtime_data = _get_runtime_data(self._hass)
        if runtime_data is None:
            body["status"] = "error"
            body["storage"] = "unavailable"
            return self.json(body, HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            # schema_version is a property that executes a real PRAGMA read —
            # proves the DB is open and answering, not just that the object
            # exists. Wrapped in a lambda so the read runs in the executor.
            schema_version = await self._hass.async_add_executor_job(
                lambda: runtime_data.storage.schema_version
            )
        except Exception:  # noqa: BLE001 — health must never 500 with a traceback
            _LOGGER.exception("Health check: storage read failed")
            body["status"] = "error"
            body["storage"] = "error"
            return self.json(body, HTTPStatus.SERVICE_UNAVAILABLE)

        body["storage"] = "ok"
        body["schema_version"] = schema_version
        return self.json(body)


# -- B1.4 entity bridge views --------------------------------------------------


class CasaSmartDevicesView(HomeAssistantView):
    """GET /api/casasmart/devices — the curated device list."""

    url = f"/api/{DOMAIN}/devices"
    name = f"api:{DOMAIN}:devices"
    # CasaSmart JWT gate (B1.6) — validated in-handler, not by HA's middleware.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Return every exposed, visible, in-scope entity as a device."""
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        rooms = claims.get("rooms")
        devices = [
            serialize_device(self._hass, state)
            for state in self._hass.states.async_all()
            if is_served(self._hass, state.entity_id)
            and in_scope(self._hass, state.entity_id, rooms)
        ]
        devices.sort(key=lambda device: device["entity_id"])
        return self.json({"devices": devices, "count": len(devices)})


class CasaSmartDeviceView(HomeAssistantView):
    """GET /api/casasmart/devices/{entity_id} — one device."""

    url = f"/api/{DOMAIN}/devices/{{entity_id}}"
    name = f"api:{DOMAIN}:device"
    requires_auth = False  # CasaSmart JWT gate (B1.6)

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Return a single device, 404 if unknown, unserved, or out of scope.

        Out-of-scope is the SAME 404 as nonexistent — a room-scoped token
        must not be able to enumerate what exists outside its rooms.
        """
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        state = self._hass.states.get(entity_id)
        if (
            state is None
            or not is_served(self._hass, entity_id)
            or not in_scope(self._hass, entity_id, claims.get("rooms"))
        ):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        return self.json(serialize_device(self._hass, state))


class CasaSmartCommandView(HomeAssistantView):
    """POST /api/casasmart/devices/{entity_id}/command — whitelisted control."""

    url = f"/api/{DOMAIN}/devices/{{entity_id}}/command"
    name = f"api:{DOMAIN}:device:command"
    requires_auth = False  # CasaSmart JWT gate (B1.6)

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request, entity_id: str) -> web.Response:
        """Validate ``{"action", "data"}`` against the whitelist and execute.

        Order matters: 404 (unknown device) before 400 (bad command), so
        probing can't distinguish "bad action" on entities that don't
        exist — and out-of-scope is the same 404 for the same reason.
        """
        claims, error = authenticate_request(
            self._hass, request, "devices.control"
        )
        if error is not None:
            return error
        state = self._hass.states.get(entity_id)
        if (
            state is None
            or not is_served(self._hass, entity_id)
            or not in_scope(self._hass, entity_id, claims.get("rooms"))
        ):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )

        try:
            payload = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON body", HTTPStatus.BAD_REQUEST)
        if not isinstance(payload, dict):
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        try:
            domain, service, service_data = validate_command(
                entity_id, payload.get("action"), payload.get("data")
            )
        except CommandError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        # The service call returns before the device confirms over Zigbee/MQTT,
        # so the state right after the call is usually stale. Listen for the
        # entity's state_changed event (registered BEFORE the call) and give
        # the device up to 2s to report — no event (e.g. turn_on on an
        # already-on light) just means nothing changed.
        changed = asyncio.Event()

        @callback
        def _on_state_changed(event: Event) -> None:
            if event.data.get("entity_id") == entity_id:
                changed.set()

        unsub = self._hass.bus.async_listen("state_changed", _on_state_changed)
        try:
            await self._hass.services.async_call(
                domain,
                service,
                {**service_data, "entity_id": entity_id},
                blocking=True,
            )
            try:
                async with asyncio.timeout(2.0):
                    await changed.wait()
            except TimeoutError:
                pass
        except HomeAssistantError as err:
            _LOGGER.warning("Command %s on %s failed: %s", service, entity_id, err)
            return self.json_message(
                f"Command failed: {err}", HTTPStatus.BAD_GATEWAY
            )
        finally:
            unsub()

        # Post-command state — confirmed fresh if the device reported in time.
        new_state = self._hass.states.get(entity_id)
        return self.json(
            {
                "ok": True,
                "entity_id": entity_id,
                "action": payload["action"],
                "device": serialize_device(self._hass, new_state)
                if new_state
                else None,
            }
        )
