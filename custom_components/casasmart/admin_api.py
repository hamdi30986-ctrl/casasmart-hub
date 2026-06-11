"""Installer admin endpoints (Track B — B16 3c-4b).

The REST surface that retires the app's last raw-HA-token call sites —
the five installer screens (pairing sheet, switch domain swap, IR
wizard, tools, discovered devices). Every endpoint here is gated by
``installer.manage`` (admin only — these reshape the home's hardware):

- ``POST /api/casasmart/admin/zigbee/permit_join`` — open/close the
  Zigbee network (the z2m permit-join publish the pairing sheet sent
  itself).
- ``GET /api/casasmart/admin/registry`` — the RAW HA entity + device
  registries (incl. hidden/disabled): what the import flows group and
  classify by.
- ``GET /api/casasmart/admin/states`` — the raw state dump behind the
  import/pairing diff (unfiltered except the token-bearing attributes —
  see ``installer.STRIPPED_STATE_ATTRS``).
- ``PATCH /api/casasmart/admin/registry/entities/{entity_id}`` — rename
  + the switch_as_x domain swap, mirroring exactly the subset of HA's
  WS ``config/entity_registry/update`` the app uses.
- ``GET/POST /api/casasmart/admin/config_flow`` and
  ``POST .../config_flow/{flow_id}`` — the config-flow proxy,
  whitelisted to the Broadlink + EasyIR handlers (``installer
  .ALLOWED_FLOW_HANDLERS``); driving any other integration's flow is a
  404 indistinguishable from nonexistence.
- ``POST /api/casasmart/admin/remote/send_command`` — IR blast through
  a ``remote.*`` entity (the remote domain is deliberately not on the
  entity-bridge whitelist).
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from aiohttp import web

from homeassistant import data_entry_flow
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
)

from .auth_api import authenticate_request, json_body
from .const import DOMAIN
from .installer import (
    ALLOWED_FLOW_HANDLERS,
    PERMIT_JOIN_TOPIC,
    InstallerError,
    filter_state_attributes,
    parse_entity_patch,
    parse_permit_join,
    parse_remote_command,
    permit_join_payload,
    serialize_flow_result,
    serialize_progress_flow,
)

_LOGGER = logging.getLogger(__name__)

# A stuck MQTT broker / IR blaster must not hang the request forever —
# same ceiling as scene activation.
_SERVICE_CALL_TIMEOUT = 10.0

_PERMISSION = "installer.manage"


class _AdminView(HomeAssistantView):
    """Shared plumbing for the installer admin views."""

    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> web.Response | None:
        """Run a service call with a timeout; an error response or None."""
        try:
            await asyncio.wait_for(
                self._hass.services.async_call(
                    domain, service, data, blocking=True
                ),
                timeout=_SERVICE_CALL_TIMEOUT,
            )
        except TimeoutError:
            _LOGGER.warning("Admin call %s.%s timed out", domain, service)
            return self.json_message(
                f"{domain}.{service} timed out", HTTPStatus.GATEWAY_TIMEOUT
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Admin call %s.%s failed: %s", domain, service, err)
            return self.json_message(
                f"{domain}.{service} failed: {err}", HTTPStatus.BAD_GATEWAY
            )
        return None


class CasaSmartAdminPermitJoinView(_AdminView):
    """POST /api/casasmart/admin/zigbee/permit_join."""

    url = f"/api/{DOMAIN}/admin/zigbee/permit_join"
    name = f"api:{DOMAIN}:admin:zigbee:permit-join"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            enable, duration = parse_permit_join(payload)
        except InstallerError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        if not self._hass.services.has_service("mqtt", "publish"):
            return self.json_message(
                "MQTT is not available on this hub",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        failed = await self._call_service(
            "mqtt",
            "publish",
            {
                "topic": PERMIT_JOIN_TOPIC,
                "payload": permit_join_payload(enable, duration),
            },
        )
        if failed is not None:
            return failed
        _LOGGER.info(
            "Zigbee permit_join %s (duration=%s)",
            "enabled" if enable else "disabled",
            duration if enable else None,
        )
        return self.json(
            {
                "ok": True,
                "enable": enable,
                "duration": duration if enable else None,
            }
        )


class CasaSmartAdminRegistryView(_AdminView):
    """GET /api/casasmart/admin/registry — raw HA registries."""

    url = f"/api/{DOMAIN}/admin/registry"
    name = f"api:{DOMAIN}:admin:registry"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        ent_reg = er.async_get(self._hass)
        dev_reg = dr.async_get(self._hass)
        entities = [
            {
                "entity_id": entry.entity_id,
                "device_id": entry.device_id,
                "entity_category": (
                    str(entry.entity_category.value)
                    if entry.entity_category is not None
                    else None
                ),
                "hidden_by": (
                    str(entry.hidden_by.value)
                    if entry.hidden_by is not None
                    else None
                ),
                "disabled_by": (
                    str(entry.disabled_by.value)
                    if entry.disabled_by is not None
                    else None
                ),
            }
            for entry in ent_reg.entities.values()
        ]
        devices = [
            {
                "id": device.id,
                "name": device.name,
                "name_by_user": device.name_by_user,
                "manufacturer": device.manufacturer,
                "model": device.model,
            }
            for device in dev_reg.devices.values()
        ]
        return self.json({"entities": entities, "devices": devices})


class CasaSmartAdminStatesView(_AdminView):
    """GET /api/casasmart/admin/states — the raw state dump."""

    url = f"/api/{DOMAIN}/admin/states"
    name = f"api:{DOMAIN}:admin:states"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        states = [
            {
                "entity_id": state.entity_id,
                "state": state.state,
                "attributes": filter_state_attributes(state.attributes),
                "last_changed": state.last_changed.isoformat(),
            }
            for state in self._hass.states.async_all()
        ]
        return self.json({"states": states, "count": len(states)})


class CasaSmartAdminEntityView(_AdminView):
    """PATCH /api/casasmart/admin/registry/entities/{entity_id}."""

    url = f"/api/{DOMAIN}/admin/registry/entities/{{entity_id}}"
    name = f"api:{DOMAIN}:admin:registry:entity"

    async def patch(self, request: web.Request, entity_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            changes = parse_entity_patch(payload)
        except InstallerError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        registry = er.async_get(self._hass)
        if registry.async_get(entity_id) is None:
            return self.json_message(
                f"Entity {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        # Mirror HA's WS handler exactly: plain updates first, then the
        # options update (the switch_as_x swap) — both raise ValueError
        # on bad input, which is the caller's 400.
        try:
            entry = registry.async_get(entity_id)
            if "name" in changes:
                entry = registry.async_update_entity(
                    entity_id, name=changes["name"]
                )
            if "options_domain" in changes:
                entry = registry.async_update_entity_options(
                    entity_id, changes["options_domain"], changes["options"]
                )
        except ValueError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(
            {
                "entity_entry": {
                    "entity_id": entry.entity_id,
                    "device_id": entry.device_id,
                    "name": entry.name,
                    "entity_category": (
                        str(entry.entity_category.value)
                        if entry.entity_category is not None
                        else None
                    ),
                }
            }
        )


class CasaSmartAdminConfigFlowsView(_AdminView):
    """GET/POST /api/casasmart/admin/config_flow — list + initiate."""

    url = f"/api/{DOMAIN}/admin/config_flow"
    name = f"api:{DOMAIN}:admin:config-flow"

    async def get(self, request: web.Request) -> web.Response:
        """In-progress flows for the whitelisted handlers only — the
        discovered-devices screen (DHCP-found Broadlink remotes)."""
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        flows = [
            serialize_progress_flow(flow)
            for flow in self._hass.config_entries.flow.async_progress()
            if flow.get("handler") in ALLOWED_FLOW_HANDLERS
        ]
        return self.json({"flows": flows})

    async def post(self, request: web.Request) -> web.Response:
        """Initiate a flow — whitelisted handlers only."""
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        handler = payload.get("handler")
        if handler not in ALLOWED_FLOW_HANDLERS:
            return self.json_message(
                "Handler not allowed", HTTPStatus.BAD_REQUEST
            )
        try:
            result = await self._hass.config_entries.flow.async_init(
                handler, context={"source": SOURCE_USER}
            )
        except data_entry_flow.UnknownHandler:
            # Whitelisted but not installed (e.g. EasyIR missing).
            return self.json_message(
                f"Integration {handler!r} is not installed on this hub",
                HTTPStatus.NOT_FOUND,
            )
        except data_entry_flow.UnknownStep as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(serialize_flow_result(result))


class CasaSmartAdminConfigFlowView(_AdminView):
    """POST /api/casasmart/admin/config_flow/{flow_id} — drive a step."""

    url = f"/api/{DOMAIN}/admin/config_flow/{{flow_id}}"
    name = f"api:{DOMAIN}:admin:config-flow:step"

    async def post(self, request: web.Request, flow_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        flow_mgr = self._hass.config_entries.flow
        # The handler gate applies to STEPS too — a flow_id minted by some
        # other integration's discovery must not be drivable through this
        # proxy. Out-of-whitelist is the same 404 as nonexistent.
        try:
            progress = flow_mgr.async_get(flow_id)
        except data_entry_flow.UnknownFlow:
            return self.json_message("Unknown flow", HTTPStatus.NOT_FOUND)
        if progress.get("handler") not in ALLOWED_FLOW_HANDLERS:
            return self.json_message("Unknown flow", HTTPStatus.NOT_FOUND)
        try:
            result = await flow_mgr.async_configure(flow_id, payload)
        except data_entry_flow.UnknownFlow:
            return self.json_message("Unknown flow", HTTPStatus.NOT_FOUND)
        except data_entry_flow.InvalidData as err:
            return self.json(
                {"errors": err.schema_errors}, HTTPStatus.BAD_REQUEST
            )
        except vol.Invalid as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(serialize_flow_result(result))


class CasaSmartAdminRemoteCommandView(_AdminView):
    """POST /api/casasmart/admin/remote/send_command — IR blast."""

    url = f"/api/{DOMAIN}/admin/remote/send_command"
    name = f"api:{DOMAIN}:admin:remote:send-command"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            entity_id, commands = parse_remote_command(payload)
        except InstallerError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        if self._hass.states.get(entity_id) is None:
            return self.json_message(
                f"Entity {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        failed = await self._call_service(
            "remote",
            "send_command",
            {"entity_id": entity_id, "command": commands},
        )
        if failed is not None:
            return failed
        return self.json(
            {"ok": True, "entity_id": entity_id, "sent": len(commands)}
        )
