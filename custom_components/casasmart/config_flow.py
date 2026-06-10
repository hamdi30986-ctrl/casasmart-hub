"""Config flow for the CasaSmart Hub integration.

One hub per HA instance (`single_config_entry` in the manifest). The flow has
no fields yet — confirm and done. Pairing/provisioning UX lands with B2/B1.6.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class CasaSmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the CasaSmart Hub config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single confirm step — no configuration to collect yet."""
        if user_input is not None:
            return self.async_create_entry(title="CasaSmart Hub", data={})

        return self.async_show_form(step_id="user")
