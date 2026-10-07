"""Automation config endpoint for the app's own automations.

GET, POST and DELETE /api/casasmart/automations/{config_key}/config let the
app edit its automations with a CasaSmart token instead of calling HA's
config API with an HA token. Only ids with the casa_automation_ prefix are
reachable, and callers need automations.manage and an unscoped token
(automations have no room). A save is checked with HA's own validator
before automations.yaml is written. Like HA's config view, a delete removes
the registry entry and does not reload.

The apps work in mireds and HA 2026 runs light actions in kelvin only, so a
save stores color_temp as color_temp_kelvin and a read gives it back in both.
A read also gives the plural keys HA's editor saves (triggers, conditions,
actions) under the singular names the apps parse.

works_during_energy_saving is kept in the hub's EnergyFlags store, not in
automations.yaml, and setting it needs energy.manage. Automation state and
on/off/trigger go through the devices feed and the command endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
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
    MAX_AUTOMATION_KEY_LENGTH,
    delete_automation,
    get_automation,
    is_casa_automation_key,
    is_valid_casa_automation_key,
    upsert_automation,
    with_singular_keys,
)
from .const import DOMAIN
from .entity_bridge import CommandError, light_data_in_kelvin, light_data_with_mireds

_LOGGER = logging.getLogger(__name__)

# Tells "field absent" apart from any value the request body could carry.
_UNSET = object()


# -- File I/O (executor-side) ----------------------------------------------------


class AutomationFileError(Exception):
    """automations.yaml exists but isn't the list HA mandates."""


def _read_yaml(path: str) -> list[dict[str, Any]]:
    """Load automations.yaml; a missing or empty file is an empty list.

    Any other non-list content raises, because reading it as empty would
    erase it on the next write.
    """
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
    """Write the list; it is serialized first so a dump error can't truncate."""
    contents = dump(data)
    write_utf8_file_atomic(path, contents)


def _shared_mutation_lock(hass: HomeAssistant) -> asyncio.Lock:
    """The automations.yaml writer lock, kept in hass.data.

    build_views makes separate view objects for HA's HTTP app and the TLS
    listener (and again on each TLS refresh), and they all write one file.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "automation_mutation_lock", asyncio.Lock()
    )


# -- Light colour temperature ---------------------------------------------------

# Both spellings HA accepts for an automation's action list.
_ACTION_KEYS = ("action", "actions")


def _map_light_data(
    node: Any, convert: Callable[[dict[str, Any]], dict[str, Any]]
) -> Any:
    """A copy of an action tree with convert applied to each light action's data.

    Every nested list and mapping is walked, so the actions inside choose, if,
    parallel, repeat and sequence blocks are included.
    """
    if isinstance(node, list):
        return [_map_light_data(item, convert) for item in node]
    if not isinstance(node, dict):
        return node
    mapped = {key: _map_light_data(value, convert) for key, value in node.items()}
    service = mapped.get("action", mapped.get("service"))
    if (
        isinstance(service, str)
        and service.startswith("light.")
        and isinstance(mapped.get("data"), dict)
    ):
        mapped["data"] = convert(mapped["data"])
    return mapped


def _map_actions(
    config: dict[str, Any], convert: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """A copy of an automation config with convert applied to its light data."""
    return {
        key: _map_light_data(value, convert) if key in _ACTION_KEYS else value
        for key, value in config.items()
    }


def _in_kelvin(data: dict[str, Any]) -> dict[str, Any]:
    """Light data with a mired color_temp in kelvin, which HA 2026 requires.

    A value that isn't a number of mireds, such as a template, is kept for HA
    to report when the automation runs.
    """
    try:
        return light_data_in_kelvin(data)
    except CommandError:
        return data


# -- The view -------------------------------------------------------------------


class CasaSmartAutomationConfigView(HomeAssistantView):
    """GET/POST/DELETE /api/casasmart/automations/{config_key}/config."""

    url = f"/api/{DOMAIN}/automations/{{config_key}}/config"
    name = f"api:{DOMAIN}:automation:config"
    requires_auth = False  # CasaSmart JWT gate

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # Saves must not interleave their read-modify-write of the file.
        self._mutation_lock = _shared_mutation_lock(hass)

    def _gate(
        self, request: web.Request
    ) -> tuple[dict[str, Any] | None, web.Response | None]:
        """Authenticate the caller and refuse room-scoped tokens."""
        claims, error = authenticate_request(self._hass, request, "automations.manage")
        if error is not None:
            return None, error
        if claims.get("rooms") is not None:
            return None, self.json_message(
                "Automation management requires an unscoped token",
                HTTPStatus.FORBIDDEN,
            )
        return claims, None

    def _check_key(self, config_key: str) -> web.Response | None:
        """A 400 unless the key is one of the app's own, well-formed ids."""
        if not is_casa_automation_key(config_key):
            return self.json_message(
                f"Not a CasaSmart automation id: {config_key!r}",
                HTTPStatus.BAD_REQUEST,
            )
        if len(config_key) > MAX_AUTOMATION_KEY_LENGTH:
            return self.json_message(
                "Invalid automation id: at most "
                f"{MAX_AUTOMATION_KEY_LENGTH} characters are allowed",
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
        """The Energy Saving flag store, or None while the hub isn't loaded."""
        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return None
        return getattr(entries[0].runtime_data, "energy_flags", None)

    async def _energy_flag(self, config_key: str, value: Any = _UNSET) -> bool:
        """Store the flag if one is given, then return the stored flag.

        False while the hub isn't loaded.
        """
        flags = self._energy_flags()
        if flags is None:
            return False
        if value is not _UNSET:
            await self._hass.async_add_executor_job(
                flags.set_works_during_energy_saving, config_key, value
            )
        return await self._hass.async_add_executor_job(
            flags.works_during_energy_saving, config_key
        )

    @property
    def _config_path(self) -> str:
        return self._hass.config.path(AUTOMATION_CONFIG_PATH)

    async def _load(
        self,
    ) -> tuple[list[dict[str, Any]] | None, web.Response | None]:
        """Read automations.yaml off the event loop; an unusable file is a 500."""
        try:
            data = await self._hass.async_add_executor_job(
                _read_yaml, self._config_path
            )
        except (AutomationFileError, HomeAssistantError) as err:
            # HA's YAML loader reports a file it cannot parse this way.
            _LOGGER.error("automations.yaml unusable: %s", err)
            return None, self.json_message(
                f"automations.yaml unusable: {err}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return data, None

    async def _save(self, data: list[dict[str, Any]]) -> web.Response | None:
        """Write automations.yaml off the event loop; a failed write is a 500."""
        try:
            await self._hass.async_add_executor_job(
                _write_yaml, self._config_path, data
            )
        except (OSError, HomeAssistantError) as err:
            # HA's atomic writer wraps every OSError in WriteError.
            _LOGGER.error("automations.yaml write failed: %s", err)
            return self.json_message(
                "Failed to persist automation", HTTPStatus.INTERNAL_SERVER_ERROR
            )
        return None

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
        # The apps' editor reads singular keys and a light's colour
        # temperature in mireds.
        value = _map_actions(with_singular_keys(value), light_data_with_mireds)
        enabled = await self._energy_flag(config_key)
        return self.json({**value, "works_during_energy_saving": enabled})

    async def post(self, request: web.Request, config_key: str) -> web.Response:
        """Create or update: validate with HA's validator, write, reload."""
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

        # The Energy Saving flag is the hub's, not part of HA's config.
        payload = dict(payload)
        energy_flag = payload.pop("works_during_energy_saving", _UNSET)
        if energy_flag is not _UNSET and not isinstance(energy_flag, bool):
            return self.json_message(
                "works_during_energy_saving must be a boolean",
                HTTPStatus.BAD_REQUEST,
            )
        if energy_flag is not _UNSET and not AuthEngine.authorize(
            claims, "energy.manage"
        ):
            return self.json_message(
                "Energy Saving flags require admin access",
                HTTPStatus.FORBIDDEN,
            )

        # The apps save light actions in mireds, which HA 2026 accepts here
        # but refuses when the automation runs.
        payload = _map_actions(payload, _in_kelvin)

        # The validator HA's config API runs. An invalid file would take
        # every automation down on the next reload.
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
            if (save_error := await self._save(data)) is not None:
                return save_error

        await self._reload(config_key)
        # Report the stored flag, even when this edit did not send one.
        effective_flag = await self._energy_flag(config_key, energy_flag)
        return self.json(
            {
                "result": "ok",
                "id": config_key,
                "works_during_energy_saving": effective_flag,
            }
        )

    async def delete(self, request: web.Request, config_key: str) -> web.Response:
        """Remove from automations.yaml and evict the registry entry."""
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
            if (save_error := await self._save(data)) is not None:
                return save_error

        # As in HA's config view: no reload. Removing the registry entry
        # (unique_id is the config key) retires the running entity, which
        # would otherwise linger in the devices feed as "unavailable".
        ent_reg = er.async_get(self._hass)
        entity_id = ent_reg.async_get_entity_id(
            AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, config_key
        )
        if entity_id is not None:
            ent_reg.async_remove(entity_id)

        flags = self._energy_flags()
        if flags is not None:
            await self._hass.async_add_executor_job(flags.delete_automation, config_key)

        return self.json({"result": "ok", "id": config_key})

    async def _reload(self, config_key: str) -> None:
        """Reload only this automation id (HA 2024.6+ scoped reload)."""
        try:
            await self._hass.services.async_call(
                AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_key}
            )
        except HomeAssistantError as err:
            # The file is already written and valid; the next reload or
            # restart picks it up.
            _LOGGER.warning("automation reload failed: %s", err)
