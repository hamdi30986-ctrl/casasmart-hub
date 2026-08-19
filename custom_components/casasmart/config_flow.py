"""CasaSmart runtime component."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLOUDFLARE_DOMAIN,
    CONF_PUSH_RELAY_URL,
    CONF_RELAY_ACTIVATION_CODE,
    CONF_RELAY_ACTIVATION_REQUEST_ID,
    CONF_TUNNEL_ENABLED,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    PUSH_RELAY_URL_CONFIG_KEY,
)
from .relay_config import normalize_relay_base_url, quiesce_relay_runtime
from .relay_registration import is_activation_code_format
from .tunnel import normalize_cloudflare_domain
from .tunnel_control import CloudflaredController, TunnelControlError

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PUSH_RELAY_URL): str,
        vol.Required(CONF_RELAY_ACTIVATION_CODE): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD,
                autocomplete="off",
            )
        ),
        vol.Optional(CONF_CLOUDFLARE_DOMAIN, default=""): str,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PUSH_RELAY_URL): str,
        vol.Optional(CONF_RELAY_ACTIVATION_CODE): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD,
                autocomplete="off",
            )
        ),
        vol.Optional(CONF_CLOUDFLARE_DOMAIN, default=""): str,
        vol.Optional(CONF_TUNNEL_ENABLED, default=False): bool,
    }
)


class CasaSmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """CasaSmart runtime component."""

    VERSION = CONFIG_ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> CasaSmartOptionsFlow:
        """CasaSmart runtime component."""
        return CasaSmartOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """CasaSmart runtime component."""
        errors: dict[str, str] = {}
        if user_input is not None:
            relay_base = normalize_relay_base_url(
                user_input.get(CONF_PUSH_RELAY_URL)
            )
            if relay_base is None:
                errors[CONF_PUSH_RELAY_URL] = "invalid_relay_url"

            activation_code = str(
                user_input.get(CONF_RELAY_ACTIVATION_CODE) or ""
            ).strip()
            if not is_activation_code_format(activation_code):
                errors[CONF_RELAY_ACTIVATION_CODE] = "invalid_activation_code"

            raw = (user_input.get(CONF_CLOUDFLARE_DOMAIN) or "").strip()
            if not raw and not errors:
                return self.async_create_entry(
                    title="CasaSmart Hub",
                    data={CONF_RELAY_ACTIVATION_CODE: activation_code},
                    options={CONF_PUSH_RELAY_URL: relay_base},
                )
            domain = normalize_cloudflare_domain(raw)
            if raw and domain is None:
                errors[CONF_CLOUDFLARE_DOMAIN] = "invalid_domain"
            elif raw and not errors:





                return self.async_create_entry(
                    title="CasaSmart Hub",
                    data={CONF_RELAY_ACTIVATION_CODE: activation_code},
                    options={
                        CONF_PUSH_RELAY_URL: relay_base,
                        CONF_CLOUDFLARE_DOMAIN: domain,
                        CONF_TUNNEL_ENABLED: True,
                    },
                )

        return self.async_show_form(
            step_id="user",


            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {
                    key: value
                    for key, value in (user_input or {}).items()
                    if key != CONF_RELAY_ACTIVATION_CODE
                },
            ),
            errors=errors,
        )


class CasaSmartOptionsFlow(OptionsFlow):
    """CasaSmart runtime component."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """CasaSmart runtime component."""
        errors: dict[str, str] = {}
        current_relay = self._current_relay_base()
        if user_input is not None:
            relay_base = normalize_relay_base_url(
                user_input.get(CONF_PUSH_RELAY_URL)
            )
            if relay_base is None:
                errors[CONF_PUSH_RELAY_URL] = "invalid_relay_url"

            activation_raw = user_input.get(CONF_RELAY_ACTIVATION_CODE)
            activation_code = (
                activation_raw.strip()
                if isinstance(activation_raw, str) and activation_raw.strip()
                else None
            )
            if activation_code is not None and not is_activation_code_format(
                activation_code
            ):
                errors[CONF_RELAY_ACTIVATION_CODE] = "invalid_activation_code"
            if (
                relay_base is not None
                and relay_base != current_relay
                and activation_code is None
            ):
                errors[CONF_RELAY_ACTIVATION_CODE] = "activation_required"

            raw = (user_input.get(CONF_CLOUDFLARE_DOMAIN) or "").strip()
            enabled = bool(user_input.get(CONF_TUNNEL_ENABLED, False))
            if not raw:
                if enabled:
                    errors["base"] = "domain_required"
            else:
                domain = normalize_cloudflare_domain(raw)
                if domain is None:
                    errors[CONF_CLOUDFLARE_DOMAIN] = "invalid_domain"
            if not errors:
                new_options = dict(self.config_entry.options)
                new_options[CONF_PUSH_RELAY_URL] = relay_base
                if raw:
                    new_options[CONF_CLOUDFLARE_DOMAIN] = domain
                    new_options[CONF_TUNNEL_ENABLED] = enabled
                else:


                    new_options.pop(CONF_CLOUDFLARE_DOMAIN, None)
                    new_options.pop(CONF_TUNNEL_ENABLED, None)

                relay_update = (
                    relay_base != current_relay or activation_code is not None
                )
                if relay_update:
                    new_data = dict(self.config_entry.data)
                    if activation_code is not None:
                        new_data[CONF_RELAY_ACTIVATION_CODE] = activation_code


                        new_data[CONF_RELAY_ACTIVATION_REQUEST_ID] = (
                            secrets.token_hex(8)
                        )
                    changed = self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data=new_data,
                        options=new_options,
                    )
                    if changed:



                        quiesce_relay_runtime(
                            getattr(self.config_entry, "runtime_data", None)
                        )
                return self.async_create_entry(data=new_options)

        options = self.config_entry.options
        suggested: dict[str, Any] = {
            CONF_PUSH_RELAY_URL: current_relay or "",
            CONF_CLOUDFLARE_DOMAIN: options.get(CONF_CLOUDFLARE_DOMAIN, ""),
            CONF_TUNNEL_ENABLED: options.get(CONF_TUNNEL_ENABLED, False),
        }
        if user_input is not None:

            suggested.update(
                {
                    key: value
                    for key, value in user_input.items()
                    if key != CONF_RELAY_ACTIVATION_CODE
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, suggested
            ),
            errors=errors,
            description_placeholders={
                "tunnel_status": await self._async_tunnel_status()
            },
        )

    def _current_relay_base(self) -> str | None:
        """CasaSmart runtime component."""
        normalized = normalize_relay_base_url(
            self.config_entry.options.get(CONF_PUSH_RELAY_URL)
        )
        if normalized is not None:
            return normalized

        runtime_data = getattr(self.config_entry, "runtime_data", None)
        applied = getattr(runtime_data, "relay_config_applied", None)
        normalized = normalize_relay_base_url(getattr(applied, "base_url", None))
        if normalized is not None:
            return normalized



        hub_config = getattr(runtime_data, "hub_config", None)
        if hub_config is not None:
            normalized = normalize_relay_base_url(
                hub_config.get(PUSH_RELAY_URL_CONFIG_KEY)
            )
            if normalized is not None:
                return normalized
        return None

    async def _async_tunnel_status(self) -> str:
        """CasaSmart runtime component."""
        controller = CloudflaredController(self.hass)
        if not controller.available():
            return (
                "Tunnel control is unavailable on this install (no add-on "
                "Supervisor) — the toggle has no effect here; manage "
                "cloudflared where it runs."
            )
        try:
            slug = await controller.async_discover()
            if slug is None:
                return (
                    "No cloudflared add-on is installed — install it and "
                    "the toggle will manage it."
                )
            state = await controller.async_state(slug)
        except TunnelControlError as err:
            return f"Tunnel add-on state unavailable right now: {err}"
        running = "running" if state.running else "stopped"
        return (
            f"Tunnel add-on: {slug} — currently {running} "
            f"(start on boot: {state.boot})."
        )
