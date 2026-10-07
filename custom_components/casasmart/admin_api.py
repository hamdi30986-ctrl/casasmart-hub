"""Installer endpoints, so the app's installer screens need no HA token.

Every endpoint needs installer.manage (admin and sub-admin). They open the
Zigbee network for pairing, return HA's raw entity and device registries and
states for the import flows, rename entities, drive the Broadlink and EasyIR
config flows, and send IR commands through remote entities, a domain the
entity bridge doesn't expose.
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
)
from homeassistant.helpers import (
    entity_registry as er,
)

from .auth_api import authenticate_request, json_body
from .const import DOMAIN, ZIGBEE_BASE_TOPICS_CONFIG_KEY
from .installer import (
    ALLOWED_FLOW_HANDLERS,
    InstallerError,
    filter_state_attributes,
    parse_entity_patch,
    parse_permit_join,
    parse_remote_command,
    permit_join_payload,
    permit_join_topic,
    resolve_zigbee_base_topics,
    serialize_flow_result,
    serialize_progress_flow,
)
from .runtime_lookup import loaded_runtime_data

_LOGGER = logging.getLogger(__name__)

# A stuck MQTT broker or IR blaster must not hang the request.
_SERVICE_CALL_TIMEOUT = 10.0

_PERMISSION = "installer.manage"


def _device_entries(dev_reg: Any) -> list[Any]:
    """Every device entry, on HA releases before and after 2026.9.

    From 2026.9 iterating devices yields entries and mapping access is
    deprecated; before, it yields ids to look up.
    """
    devices = dev_reg.devices
    return [devices[item] if isinstance(item, str) else item for item in devices]


def _enum_value(member: Any) -> str | None:
    """A registry enum member's value as a string, or None."""
    return str(member.value) if member is not None else None


class _AdminView(HomeAssistantView):
    """Shared plumbing for the installer admin views."""

    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> web.Response | None:
        """Call a service with a timeout; returns an error response or None."""
        try:
            await asyncio.wait_for(
                self._hass.services.async_call(domain, service, data, blocking=True),
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
        # permit_join is per zigbee2mqtt instance and a large home may run one
        # per floor, so open every configured instance unless one is named.
        topics = resolve_zigbee_base_topics(
            self._zigbee_base_topics(), payload.get("base_topic")
        )
        message = permit_join_payload(enable, duration)
        for base_topic in topics:
            failed = await self._call_service(
                "mqtt",
                "publish",
                {
                    "topic": permit_join_topic(base_topic),
                    "payload": message,
                },
            )
            if failed is not None:
                return failed
        _LOGGER.info(
            "Zigbee permit_join %s (duration=%s) on %s",
            "enabled" if enable else "disabled",
            duration if enable else None,
            ", ".join(topics),
        )
        return self.json(
            {
                "ok": True,
                "enable": enable,
                "duration": duration if enable else None,
                "instances": topics,
            }
        )

    def _zigbee_base_topics(self) -> Any:
        """The hub's configured zigbee2mqtt base topics (None when unset)."""
        runtime_data = loaded_runtime_data(self._hass)
        if runtime_data is None:
            return None
        return runtime_data.hub_config.get(ZIGBEE_BASE_TOPICS_CONFIG_KEY)


class CasaSmartAdminRegistryView(_AdminView):
    """GET /api/casasmart/admin/registry: HA's raw entity and device registries."""

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
                "entity_category": _enum_value(entry.entity_category),
                "hidden_by": _enum_value(entry.hidden_by),
                "disabled_by": _enum_value(entry.disabled_by),
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
            for device in _device_entries(dev_reg)
        ]
        return self.json({"entities": entities, "devices": devices})


class CasaSmartAdminStatesView(_AdminView):
    """GET /api/casasmart/admin/states: every state, minus token attributes."""

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
    """PATCH /api/casasmart/admin/registry/entities/{entity_id}: rename."""

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
        # parse_entity_patch only allows a name.
        try:
            entry = registry.async_update_entity(entity_id, name=changes["name"])
        except ValueError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(
            {
                "entity_entry": {
                    "entity_id": entry.entity_id,
                    "device_id": entry.device_id,
                    "name": entry.name,
                    "entity_category": _enum_value(entry.entity_category),
                }
            }
        )


class CasaSmartAdminConfigFlowsView(_AdminView):
    """GET/POST /api/casasmart/admin/config_flow: list or start a flow."""

    url = f"/api/{DOMAIN}/admin/config_flow"
    name = f"api:{DOMAIN}:admin:config-flow"

    async def get(self, request: web.Request) -> web.Response:
        """In-progress flows of the allowed handlers (discovered devices)."""
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
        """Start a flow for an allowed handler."""
        _, error = authenticate_request(self._hass, request, _PERMISSION)
        if error is not None:
            return error
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        handler = payload.get("handler")
        if not isinstance(handler, str) or handler not in ALLOWED_FLOW_HANDLERS:
            return self.json_message("Handler not allowed", HTTPStatus.BAD_REQUEST)
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
    """POST /api/casasmart/admin/config_flow/{flow_id}: submit a step."""

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
        # Another integration's flow gets the same 404 as an unknown one.
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
            return self.json({"errors": err.schema_errors}, HTTPStatus.BAD_REQUEST)
        except vol.Invalid as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(serialize_flow_result(result))


class CasaSmartAdminRemoteCommandView(_AdminView):
    """POST /api/casasmart/admin/remote/send_command: send IR commands."""

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
        return self.json({"ok": True, "entity_id": entity_id, "sent": len(commands)})
