"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import logging
import os
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from aiohttp import web

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.components.automation.config import (
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
from .auth_engine import AuthEngine
from .automations import (
    CASA_AUTOMATION_PREFIX,
    delete_automation,
    get_automation,
    is_casa_automation_key,
    is_valid_casa_automation_key,
    upsert_automation,
)
from .const import DOMAIN

_UNSET = object()

_LOGGER = logging.getLogger(__name__)





class AutomationFileError(Exception):
    """CasaSmart runtime component."""


def _read_yaml(path: str) -> list[dict[str, Any]]:
    """CasaSmart runtime component."""
    if not os.path.isfile(path):
        return []
    content = load_yaml(path)
    if content is None:

        return []
    if not isinstance(content, list):
        raise AutomationFileError(
            f"automations.yaml is {type(content).__name__}, expected a list"
        )
    return content


def _write_yaml(path: str, data: list[dict[str, Any]]) -> None:
    """CasaSmart runtime component."""
    contents = dump(data)
    write_utf8_file_atomic(path, contents)





class CasaSmartAutomationConfigView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/automations/{{config_key}}/config"
    name = f"api:{DOMAIN}:automation:config"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass



        self._mutation_lock = asyncio.Lock()

    def _gate(
        self, request: web.Request
    ) -> tuple[dict[str, Any] | None, web.Response | None]:
        """CasaSmart runtime component."""
        claims, error = authenticate_request(
            self._hass, request, "automations.manage"
        )
        if error is not None:
            return None, error
        if claims.get("rooms") is not None:


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
        if not is_valid_casa_automation_key(config_key):



            return self.json_message(
                f"Invalid automation id {config_key!r}: only letters, digits "
                "and underscores are allowed after the "
                f"{CASA_AUTOMATION_PREFIX!r} prefix",
                HTTPStatus.BAD_REQUEST,
            )
        return None

    def _energy_flags(self):
        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return None
        return getattr(entries[0].runtime_data, "energy_flags", None)

    @property
    def _config_path(self) -> str:
        return self._hass.config.path(AUTOMATION_CONFIG_PATH)

    async def _load(
        self,
    ) -> tuple[list[dict[str, Any]] | None, web.Response | None]:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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
        flags = self._energy_flags()
        enabled = False
        if flags is not None:
            enabled = await self._hass.async_add_executor_job(
                flags.works_during_energy_saving, config_key
            )
        return self.json(
            {**value, "works_during_energy_saving": enabled}
        )

    async def post(self, request: web.Request, config_key: str) -> web.Response:
        """CasaSmart runtime component."""
        claims, error = self._gate(request)
        if error is not None:
            return error
        if (bad := self._check_key(config_key)) is not None:
            return bad
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )

        payload = dict(payload)
        energy_flag = payload.pop("works_during_energy_saving", _UNSET)
        if energy_flag is not _UNSET and not isinstance(energy_flag, bool):
            return self.json_message(
                "works_during_energy_saving must be a boolean",
                HTTPStatus.BAD_REQUEST,
            )
        if (
            energy_flag is not _UNSET
            and not AuthEngine.authorize(claims, "energy.manage")
        ):
            return self.json_message(
                "Energy Saving flags require admin access",
                HTTPStatus.FORBIDDEN,
            )




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
        flags = self._energy_flags()
        effective_flag = False
        if flags is not None and energy_flag is not _UNSET:
            await self._hass.async_add_executor_job(
                flags.set_works_during_energy_saving,
                config_key,
                energy_flag,
            )
        if flags is not None:
            effective_flag = await self._hass.async_add_executor_job(
                flags.works_during_energy_saving, config_key
            )
        return self.json(
            {
                "result": "ok",
                "id": config_key,
                "works_during_energy_saving": effective_flag,
            }
        )

    async def delete(self, request: web.Request, config_key: str) -> web.Response:
        """CasaSmart runtime component."""
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




        ent_reg = er.async_get(self._hass)
        entity_id = ent_reg.async_get_entity_id(
            AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, config_key
        )
        if entity_id is not None:
            ent_reg.async_remove(entity_id)

        flags = self._energy_flags()
        if flags is not None:
            await self._hass.async_add_executor_job(
                flags.delete_automation, config_key
            )

        return self.json({"result": "ok", "id": config_key})

    async def _reload(self, config_key: str) -> None:
        """CasaSmart runtime component."""
        try:
            await self._hass.services.async_call(
                AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_key}
            )
        except HomeAssistantError as err:



            _LOGGER.warning("automation reload failed: %s", err)
