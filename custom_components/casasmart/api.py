"""Core CasaSmart REST views and the list of every view the hub serves.

Handshake, health, devices, device commands and history live here;
build_views collects them with the other *_api modules' views.

Views set requires_auth = False because they don't use HA's own tokens: each
handler checks the CasaSmart JWT itself with authenticate_request.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from http import HTTPStatus
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .admin_api import (
    CasaSmartAdminConfigFlowsView,
    CasaSmartAdminConfigFlowView,
    CasaSmartAdminEntityView,
    CasaSmartAdminPermitJoinView,
    CasaSmartAdminRegistryView,
    CasaSmartAdminRemoteCommandView,
    CasaSmartAdminStatesView,
)
from .alarm_api import (
    CasaSmartAlarmArmView,
    CasaSmartAlarmDisarmView,
    CasaSmartAlarmHistoryView,
    CasaSmartAlarmSettingsView,
    CasaSmartAlarmStateView,
    CasaSmartAlarmZonesView,
    CasaSmartAlarmZoneView,
)
from .audio_api import (
    CasaSmartAudioAirplayView,
    CasaSmartAudioAthanView,
    CasaSmartAudioBroadcastView,
    CasaSmartAudioBrokerView,
    CasaSmartAudioCommandView,
    CasaSmartAudioDiscoverView,
    CasaSmartAudioPaClipView,
    CasaSmartAudioPaConfigView,
    CasaSmartAudioPaView,
    CasaSmartAudioProvisionView,
    CasaSmartAudioSpeakersView,
    CasaSmartAudioSpeakerView,
)
from .auth_api import (
    CasaSmartChallengeView,
    CasaSmartEnrollView,
    CasaSmartPairingCodesView,
    CasaSmartPairingCodeView,
    CasaSmartRecoverView,
    CasaSmartTokenView,
    CasaSmartUnpairSelfView,
    CasaSmartUsersView,
    CasaSmartUserView,
    CasaSmartWhoamiView,
    CasaSmartWidgetTokenView,
    async_member_id,
    authenticate_request,
    is_lan_request,
)
from .automation_api import CasaSmartAutomationConfigView
from .camera_api import (
    CasaSmartCameraHlsProxyView,
    CasaSmartCameraSnapshotView,
    CasaSmartCameraStreamView,
)
from .capabilities import handshake_capabilities
from .const import (
    API_VERSION,
    API_VERSION_HEADER,
    DOMAIN,
    HUB_NAME_CONFIG_KEY,
    MIN_APP_VERSION,
    SUPPORTED_API_VERSIONS,
)
from .discovery import DEFAULT_HUB_NAME
from .energy_api import (
    CasaSmartEnergyActivateView,
    CasaSmartEnergyConfigView,
    CasaSmartEnergyDeactivateView,
    CasaSmartEnergyDiscoveryView,
    CasaSmartEnergyReapplyView,
    CasaSmartEnergyStateView,
)
from .energy_runtime import energy_lockout_applies
from .entity_bridge import CommandError, validate_command
from .filtering import in_scope, is_served, serialize_device
from .history import (
    HistoryQueryError,
    parse_history_query,
    serialize_history,
)
from .now_api import (
    CasaSmartNowConfigView,
    CasaSmartNowView,
    CasaSmartRoomActivityCommandView,
    CasaSmartRoomActivityPolicyView,
)
from .push_api import CasaSmartHqNotificationView, CasaSmartPushTokenView
from .registry_api import (
    CasaSmartDeviceAssignmentView,
    CasaSmartFavoritesView,
    CasaSmartFloorsView,
    CasaSmartFloorView,
    CasaSmartRegistryView,
    CasaSmartRoomMoveView,
    CasaSmartRoomsView,
    CasaSmartRoomTagsView,
    CasaSmartRoomTagView,
    CasaSmartRoomView,
    CasaSmartSceneActivateView,
    CasaSmartScenesView,
    CasaSmartSceneView,
    CasaSmartUserDeviceGangView,
    CasaSmartUserDeviceView,
)
from .runtime_lookup import loaded_runtime_data
from .settings_api import CasaSmartUserSettingsView
from .suggestion_api import (
    CasaSmartGeneratedSuggestionActionView,
    CasaSmartGeneratedSuggestionsView,
    CasaSmartSuggestionActionView,
    CasaSmartSuggestionPreviewView,
    CasaSmartSuggestionRulesView,
    CasaSmartSuggestionsView,
)
from .tank_api import (
    CasaSmartTankCalibrationView,
    CasaSmartTankDevicesView,
    CasaSmartTankDeviceView,
    CasaSmartTankProvisionView,
    CasaSmartTankReadingsView,
    CasaSmartTankReadingView,
    CasaSmartTankStatusView,
)
from .tunnel import TUNNEL_URL_CONFIG_KEY, normalize_tunnel_url
from .update_api import (
    CasaSmartUpdateInstallView,
    CasaSmartUpdateStatusView,
    get_or_create_checker,
)
from .ws import CasaSmartWebSocketView

_LOGGER = logging.getLogger(__name__)


def _visible_state(
    hass: HomeAssistant, entity_id: str, rooms: list[str] | None
) -> State | None:
    """The entity's state when it exists, is served and is in rooms, else None."""
    state = hass.states.get(entity_id)
    if (
        state is None
        or not is_served(hass, entity_id)
        or not in_scope(hass, entity_id, rooms)
    ):
        return None
    return state


def build_views(hass: HomeAssistant, hub_version: str) -> list[HomeAssistantView]:
    """A fresh instance of every view the hub serves.

    Called for HA's HTTP server and again for the hub's TLS listener, so state
    the listeners share lives in hass.data, never on a view.
    """
    return [
        CasaSmartHandshakeView(hass, hub_version),
        CasaSmartHealthView(hass, hub_version),
        CasaSmartDevicesView(hass),
        CasaSmartDeviceView(hass),
        CasaSmartCommandView(hass),
        CasaSmartHistoryView(hass),
        CasaSmartEnergyStateView(hass),
        CasaSmartEnergyDiscoveryView(hass),
        CasaSmartEnergyConfigView(hass),
        CasaSmartEnergyActivateView(hass),
        CasaSmartEnergyDeactivateView(hass),
        CasaSmartEnergyReapplyView(hass),
        CasaSmartAutomationConfigView(hass),
        CasaSmartCameraSnapshotView(hass),
        CasaSmartCameraStreamView(hass),
        CasaSmartCameraHlsProxyView(hass),
        CasaSmartWebSocketView(hass, hub_version),
        CasaSmartEnrollView(hass),
        CasaSmartRecoverView(hass),
        CasaSmartChallengeView(hass),
        CasaSmartTokenView(hass),
        CasaSmartWhoamiView(hass),
        CasaSmartUnpairSelfView(hass),
        CasaSmartWidgetTokenView(hass),
        CasaSmartPairingCodesView(hass),
        CasaSmartPairingCodeView(hass),
        CasaSmartUsersView(hass),
        CasaSmartUserView(hass),
        CasaSmartPushTokenView(hass),
        CasaSmartHqNotificationView(hass),
        CasaSmartRegistryView(hass),
        CasaSmartFloorsView(hass),
        CasaSmartFloorView(hass),
        CasaSmartRoomsView(hass),
        CasaSmartRoomView(hass),
        CasaSmartRoomTagsView(hass),
        CasaSmartRoomTagView(hass),
        CasaSmartDeviceAssignmentView(hass),
        CasaSmartRoomMoveView(hass),
        CasaSmartUserDeviceView(hass),
        CasaSmartUserDeviceGangView(hass),
        CasaSmartScenesView(hass),
        CasaSmartSceneView(hass),
        CasaSmartSceneActivateView(hass),
        CasaSmartFavoritesView(hass),
        CasaSmartUserSettingsView(hass),
        CasaSmartNowView(hass),
        CasaSmartNowConfigView(hass),
        CasaSmartSuggestionsView(hass),
        CasaSmartGeneratedSuggestionsView(hass),
        CasaSmartGeneratedSuggestionActionView(hass),
        CasaSmartSuggestionRulesView(hass),
        CasaSmartSuggestionPreviewView(hass),
        CasaSmartSuggestionActionView(hass),
        CasaSmartRoomActivityPolicyView(hass),
        CasaSmartRoomActivityCommandView(hass),
        CasaSmartUpdateStatusView(hass, get_or_create_checker(hass, hub_version)),
        CasaSmartUpdateInstallView(hass, get_or_create_checker(hass, hub_version)),
        CasaSmartAlarmStateView(hass),
        CasaSmartAlarmArmView(hass),
        CasaSmartAlarmDisarmView(hass),
        CasaSmartAlarmZonesView(hass),
        CasaSmartAlarmZoneView(hass),
        CasaSmartAlarmSettingsView(hass),
        CasaSmartAlarmHistoryView(hass),
        CasaSmartTankProvisionView(hass),
        CasaSmartTankReadingView(hass),
        CasaSmartTankDevicesView(hass),
        CasaSmartTankDeviceView(hass),
        CasaSmartTankReadingsView(hass),
        CasaSmartTankCalibrationView(hass),
        CasaSmartTankStatusView(hass),
        CasaSmartAudioSpeakersView(hass),
        CasaSmartAudioSpeakerView(hass),
        CasaSmartAudioDiscoverView(hass),
        CasaSmartAudioCommandView(hass),
        CasaSmartAudioAirplayView(hass),
        CasaSmartAudioBroadcastView(hass),
        CasaSmartAudioPaView(hass),
        CasaSmartAudioPaClipView(hass),
        CasaSmartAudioAthanView(hass),
        CasaSmartAudioBrokerView(hass),
        CasaSmartAudioPaConfigView(hass),
        CasaSmartAudioProvisionView(hass),
        CasaSmartAdminPermitJoinView(hass),
        CasaSmartAdminRegistryView(hass),
        CasaSmartAdminStatesView(hass),
        CasaSmartAdminEntityView(hass),
        CasaSmartAdminConfigFlowsView(hass),
        CasaSmartAdminConfigFlowView(hass),
        CasaSmartAdminRemoteCommandView(hass),
    ]


def async_register_views(hass: HomeAssistant, hub_version: str) -> None:
    """Register the views once; HA's router can't unregister them on reload."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("views_registered"):
        return
    for view in build_views(hass, hub_version):
        hass.http.register_view(view)
    domain_data["views_registered"] = True
    _LOGGER.debug("CasaSmart REST + WS views registered")


class CasaSmartHandshakeView(HomeAssistantView):
    """GET /api/casasmart/handshake: the unauthenticated discovery probe.

    Reports the API and hub versions and the capabilities, plus the TLS
    identity the app pins and the tunnel URL when configured. A LAN caller
    also gets the hub's name. With the API version header it also says
    whether that version is supported.
    """

    url = f"/api/{DOMAIN}/handshake"
    name = f"api:{DOMAIN}:handshake"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version
        # The app also polls the handshake for reachability, so warn once.
        self._tunnel_warned = False

    async def get(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = {
            "api_version": API_VERSION,
            "min_app_version": MIN_APP_VERSION,
            "hub_version": self._hub_version,
            "supported_api_versions": list(SUPPORTED_API_VERSIONS),
            # Apps check features here instead of inferring them from
            # api_version; older apps ignore the key.
            "capabilities": handshake_capabilities(),
        }

        runtime_data = loaded_runtime_data(self._hass)
        # A phone that finds the hub by scanning the LAN shows this name. The
        # handshake is public through the tunnel, so the name stays on the LAN.
        if is_lan_request(request):
            hub_name = (
                runtime_data.hub_config.get(HUB_NAME_CONFIG_KEY)
                if runtime_data
                else None
            )
            if isinstance(hub_name, str) and hub_name.strip():
                body["hub_name"] = hub_name.strip()
            else:
                body["hub_name"] = DEFAULT_HUB_NAME

        if runtime_data is not None and runtime_data.tls is not None:
            body["tls"] = {
                "port": runtime_data.tls.port,
                "identity_key": runtime_data.tls.material.identity_public_pem,
                "identity_fingerprint_sha256": (
                    runtime_data.tls.material.identity_fingerprint
                ),
            }

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
    """GET /api/casasmart/health: liveness probe for external monitoring."""

    url = f"/api/{DOMAIN}/health"
    name = f"api:{DOMAIN}:health"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.Response:
        """200 when the entry is loaded and storage answers a read, else 503."""
        body: dict[str, Any] = {
            "status": "ok",
            "hub_version": self._hub_version,
            "api_version": API_VERSION,
        }

        runtime_data = loaded_runtime_data(self._hass)
        if runtime_data is None:
            body["status"] = "error"
            body["storage"] = "unavailable"
            return self.json(body, HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            # schema_version runs a real PRAGMA read.
            schema_version = await self._hass.async_add_executor_job(
                lambda: runtime_data.storage.schema_version
            )
        except Exception:
            _LOGGER.exception("Health check: storage read failed")
            body["status"] = "error"
            body["storage"] = "error"
            return self.json(body, HTTPStatus.SERVICE_UNAVAILABLE)

        body["storage"] = "ok"
        body["schema_version"] = schema_version
        return self.json(body)


class CasaSmartDevicesView(HomeAssistantView):
    """GET /api/casasmart/devices: every device the caller may see."""

    url = f"/api/{DOMAIN}/devices"
    name = f"api:{DOMAIN}:devices"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Every served, in-scope entity as a device."""
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
    """GET /api/casasmart/devices/{entity_id}: one device."""

    url = f"/api/{DOMAIN}/devices/{{entity_id}}"
    name = f"api:{DOMAIN}:device"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """One device; out of scope gets the same 404 as unknown."""
        claims, error = authenticate_request(self._hass, request, "devices.read")
        if error is not None:
            return error
        state = _visible_state(self._hass, entity_id, claims.get("rooms"))
        if state is None:
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        return self.json(serialize_device(self._hass, state))


class CasaSmartCommandView(HomeAssistantView):
    """POST /api/casasmart/devices/{entity_id}/command: one whitelisted action.

    The reply carries the device's state after the command, and a successful
    command is recorded in the member's recently used devices.
    """

    url = f"/api/{DOMAIN}/devices/{{entity_id}}/command"
    name = f"api:{DOMAIN}:device:command"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request, entity_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "devices.control")
        if error is not None:
            return error
        if _visible_state(self._hass, entity_id, claims.get("rooms")) is None:
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )

        runtime_data = loaded_runtime_data(self._hass)
        energy = getattr(runtime_data, "energy", None)
        if energy is not None and energy_lockout_applies(energy, claims):
            return self.json(
                {
                    "error": "energy_lockout",
                    "message": (
                        "Energy saving is active — controls are locked by the admin"
                    ),
                    # The phone reads code on a 403: this isn't an expired login.
                    "code": "energy_lockout",
                },
                HTTPStatus.FORBIDDEN,
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

        # Listen before the call so a change during it isn't missed, then wait
        # up to 2 s for the new state to include in the reply.
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
            return self.json_message(f"Command failed: {err}", HTTPStatus.BAD_GATEWAY)
        finally:
            unsub()

        new_state = self._hass.states.get(entity_id)
        now_data = getattr(loaded_runtime_data(self._hass), "now_data", None)
        if now_data is not None:
            try:
                member_id = await async_member_id(self._hass, claims)
                await self._hass.async_add_executor_job(
                    now_data.record_successful_control, member_id, entity_id
                )
            except Exception:
                # The command succeeded; don't fail it over the recents list.
                _LOGGER.exception("Now recency recording failed for %s", entity_id)
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


class CasaSmartHistoryView(HomeAssistantView):
    """GET /api/casasmart/history: recorder history for served entities.

    Ids that are unknown, not served or outside the caller's rooms are left
    out of the query and the reply alike, so they can't be told apart.
    """

    url = f"/api/{DOMAIN}/history"
    name = f"api:{DOMAIN}:history"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "history.read")
        if error is not None:
            return error

        try:
            entity_ids, start, end, significant = parse_history_query(
                request.query, now=dt_util.utcnow()
            )
        except HistoryQueryError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        rooms = claims.get("rooms")

        allowed = [
            entity_id
            for entity_id in entity_ids
            if _visible_state(self._hass, entity_id, rooms) is not None
        ]
        if not allowed:
            return self.json({"history": {}})

        try:
            recorder = get_instance(self._hass)
        except (KeyError, AttributeError):
            return self.json_message(
                "History unavailable: recorder not running",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

        states = await recorder.async_add_executor_job(
            partial(
                get_significant_states,
                self._hass,
                start,
                end,
                allowed,
                include_start_time_state=True,
                significant_changes_only=significant,
                # HA's minimal form drops samples after the first (meter
                # counters, for one), and the app needs every row.
                minimal_response=False,
                no_attributes=True,
            )
        )
        return self.json({"history": serialize_history(allowed, states)})
