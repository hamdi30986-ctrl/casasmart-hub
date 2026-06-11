"""Automation config endpoints (Track B — B16 stage 3c-3).

The REST surface that replaces the app's raw-token calls to HA's own
``/api/config/automation/config/{id}`` (create / edit / delete) and the
entity-registry ghost cleanup that followed a delete:

- ``GET    /api/casasmart/automations/{config_key}/config`` — one
  automation's config, for the editor's lazy load.
- ``POST   /api/casasmart/automations/{config_key}/config`` — create or
  update; the config is validated with HA's OWN automation validator
  before a byte is written, then automations.yaml is rewritten
  atomically and the automation component reloads just that id.
- ``DELETE /api/casasmart/automations/{config_key}/config`` — remove
  from automations.yaml, reload, and clean the ghost entity out of HA's
  entity registry (the hub does the cleanup the app used to do with a
  second raw-token call).

All three sit behind ``automations.manage`` (admin + sub-admin — family
members toggle and trigger through the command endpoint, they never
rewrite configs), and all three refuse room-scoped tokens outright:
automations have no room, so a scoped grant cannot contain them.

The surface is scoped to the app's own automations — config keys MUST
carry the ``casa_automation_`` prefix. Hub-internal and installer
automations are not reachable here, in either direction.

Automation STATE (on/off, last_triggered) deliberately does not live
here: automation is an exposed domain in ``entity_bridge``, so state
rides the same devices/WS feed as everything else, and enable/disable/
trigger ride the whitelisted command endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from aiohttp import web

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.components.automation.config import (  # noqa: PLC2701 — the same validator HA's own config API uses
    async_validate_config_item,
)
from homeassistant.components.http import HomeAssistantView
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from .auth_api import authenticate_request, json_body
from .automations import (
    delete_automation,
    get_automation,
    is_casa_automation_key,
    upsert_automation,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# -- File I/O (executor-side) ----------------------------------------------------


class AutomationFileError(Exception):
    """automations.yaml exists but isn't the list HA mandates."""


def _read_yaml(path: str) -> list[dict[str, Any]]:
    """Load automations.yaml; a missing or empty file is an empty list.

    A present-but-non-list file (hand-edited into a dict/scalar) raises
    instead of coercing to ``[]`` — silently treating corrupt content as
    empty would DISCARD it on the next write (audit finding, 3c-3).
    """
    if not os.path.isfile(path):
        return []
    content = load_yaml(path)
    if content is None:
        # All-comments / empty file — legitimately no automations.
        return []
    if not isinstance(content, list):
        raise AutomationFileError(
            f"automations.yaml is {type(content).__name__}, expected a list"
        )
    return content


def _write_yaml(path: str, data: list[dict[str, Any]]) -> None:
    """Serialize BEFORE opening the file — a dump error must not truncate."""
    contents = dump(data)
    write_utf8_file_atomic(path, contents)


# -- The view -------------------------------------------------------------------


class CasaSmartAutomationConfigView(HomeAssistantView):
    """GET/POST/DELETE /api/casasmart/automations/{config_key}/config."""

    url = f"/api/{DOMAIN}/automations/{{config_key}}/config"
    name = f"api:{DOMAIN}:automation:config"
    requires_auth = False  # CasaSmart JWT gate (B1.6)

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # One writer at a time — concurrent saves must not interleave the
        # read-modify-write on automations.yaml (same lock discipline as
        # HA's own config view).
        self._mutation_lock = asyncio.Lock()

    def _gate(
        self, request: web.Request
    ) -> tuple[dict[str, Any] | None, web.Response | None]:
        """Auth + scope + ownership, shared by all three verbs."""
        claims, error = authenticate_request(
            self._hass, request, "automations.manage"
        )
        if error is not None:
            return None, error
        if claims.get("rooms") is not None:
            # Room-scoped tokens have no business in config-land, whatever
            # their role claims — automations don't live in a room.
            return None, self.json_message(
                "Automation management requires an unscoped token",
                HTTPStatus.FORBIDDEN,
            )
        return claims, None

    def _check_key(self, config_key: str) -> web.Response | None:
        if not is_casa_automation_key(config_key):
            return self.json_message(
                f"Not a CasaSmart automation id: {config_key!r}",
                HTTPStatus.BAD_REQUEST,
            )
        return None

    @property
    def _config_path(self) -> str:
        return self._hass.config.path(AUTOMATION_CONFIG_PATH)

    async def _load(
        self,
    ) -> tuple[list[dict[str, Any]] | None, web.Response | None]:
        """Read automations.yaml off-loop; a corrupt file is a 500, never
        an empty list a later write would clobber."""
        try:
            data = await self._hass.async_add_executor_job(
                _read_yaml, self._config_path
            )
        except AutomationFileError as err:
            _LOGGER.error("automations.yaml unusable: %s", err)
            return None, self.json_message(
                f"automations.yaml unusable: {err}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return data, None

    async def get(self, request: web.Request, config_key: str) -> web.Response:
        """One automation's stored config (the editor's lazy load)."""
        _, error = self._gate(request)
        if error is not None:
            return error
        if (bad := self._check_key(config_key)) is not None:
            return bad
        async with self._mutation_lock:
            data, error = await self._load()
            if error is not None:
                return error
        value = get_automation(data, config_key)
        if value is None:
            return self.json_message(
                f"Automation {config_key!r} not found", HTTPStatus.NOT_FOUND
            )
        return self.json(value)

    async def post(self, request: web.Request, config_key: str) -> web.Response:
        """Create or update — validate with HA's validator, write, reload."""
        _, error = self._gate(request)
        if error is not None:
            return error
        if (bad := self._check_key(config_key)) is not None:
            return bad
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        # HA's own validator — the exact gate its native config API runs.
        # Invalid configs die here, BEFORE automations.yaml is touched: a
        # bad write would take every automation down on the next reload.
        try:
            await async_validate_config_item(self._hass, config_key, dict(payload))
        except (vol.Invalid, HomeAssistantError) as err:
            return self.json_message(
                f"Invalid automation config: {err}", HTTPStatus.BAD_REQUEST
            )

        async with self._mutation_lock:
            data, load_error = await self._load()
            if load_error is not None:
                return load_error
            upsert_automation(data, config_key, payload)
            try:
                await self._hass.async_add_executor_job(
                    _write_yaml, self._config_path, data
                )
            except OSError as err:
                _LOGGER.error("automations.yaml write failed: %s", err)
                return self.json_message(
                    "Failed to persist automation", HTTPStatus.INTERNAL_SERVER_ERROR
                )

        await self._reload(config_key)
        return self.json({"result": "ok", "id": config_key})

    async def delete(self, request: web.Request, config_key: str) -> web.Response:
        """Remove from automations.yaml, reload, evict the ghost entity."""
        _, error = self._gate(request)
        if error is not None:
            return error
        if (bad := self._check_key(config_key)) is not None:
            return bad

        async with self._mutation_lock:
            data, load_error = await self._load()
            if load_error is not None:
                return load_error
            if not delete_automation(data, config_key):
                return self.json_message(
                    f"Automation {config_key!r} not found", HTTPStatus.NOT_FOUND
                )
            try:
                await self._hass.async_add_executor_job(
                    _write_yaml, self._config_path, data
                )
            except OSError as err:
                _LOGGER.error("automations.yaml write failed: %s", err)
                return self.json_message(
                    "Failed to persist automation", HTTPStatus.INTERNAL_SERVER_ERROR
                )

        # The cleanup HA's own config API does on delete: the registry
        # entry (unique_id == config key) lingers after the config is
        # gone and would haunt the entities feed as "unavailable".
        ent_reg = er.async_get(self._hass)
        entity_id = ent_reg.async_get_entity_id(
            AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, config_key
        )
        if entity_id is not None:
            ent_reg.async_remove(entity_id)

        return self.json({"result": "ok", "id": config_key})

    async def _reload(self, config_key: str) -> None:
        """Reload just this automation id (HA 2024.6+ scoped reload)."""
        try:
            await self._hass.services.async_call(
                AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_key}
            )
        except HomeAssistantError as err:
            # The yaml is already written and valid — a reload hiccup
            # self-heals on the next reload/restart. Log, don't fail the
            # request that did its job.
            _LOGGER.warning("automation reload failed: %s", err)
