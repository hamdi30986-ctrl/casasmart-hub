"""Config flow for the CasaSmart Hub integration.

One hub per HA instance (`single_config_entry` in the manifest). Setup asks
one optional question — the hub's Cloudflare tunnel domain. Providing it
seeds ``tunnel_enabled: True`` in the entry options, which the reconciler
in ``__init__.py`` enforces by starting the cloudflared add-on: cloud stays
on from day one (pairing redesign Phase 2). Pairing does not need the
tunnel down — the app pairs against the hub's LAN address directly (mDNS →
pinned TLS), never via the Cloudflare domain, and the enroll gate keeps its
own policy (LAN-only unless ``remote_pairing_enabled``, ``auth_api.py``).
The gear-icon options flow edits the domain and keeps the on/off toggle as
a manual emergency switch — the hub never flips it off by itself.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import CONF_CLOUDFLARE_DOMAIN, CONF_TUNNEL_ENABLED, DOMAIN
from .tunnel import normalize_cloudflare_domain
from .tunnel_control import CloudflaredController, TunnelControlError

STEP_USER_SCHEMA = vol.Schema(
    {vol.Optional(CONF_CLOUDFLARE_DOMAIN, default=""): str}
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CLOUDFLARE_DOMAIN, default=""): str,
        vol.Optional(CONF_TUNNEL_ENABLED, default=False): bool,
    }
)


class CasaSmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the CasaSmart Hub config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> CasaSmartOptionsFlow:
        """Gear icon: edit the Cloudflare domain / toggle the tunnel."""
        return CasaSmartOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single step — optional Cloudflare tunnel domain, then done."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = (user_input.get(CONF_CLOUDFLARE_DOMAIN) or "").strip()
            if not raw:
                # No tunnel on this install — exactly today's behavior.
                return self.async_create_entry(title="CasaSmart Hub", data={})
            domain = normalize_cloudflare_domain(raw)
            if domain is None:
                errors[CONF_CLOUDFLARE_DOMAIN] = "invalid_domain"
            else:
                # Phase 2: cloud stays on. Seeding True (not omitting the
                # key — the reconciler defaults an absent key to False and
                # would stop the add-on) means the first reconcile after
                # setup starts cloudflared + boot=auto. The options toggle
                # below remains the manual emergency off-switch.
                return self.async_create_entry(
                    title="CasaSmart Hub",
                    data={},
                    options={
                        CONF_CLOUDFLARE_DOMAIN: domain,
                        CONF_TUNNEL_ENABLED: True,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
        )


class CasaSmartOptionsFlow(OptionsFlow):
    """Edit the Cloudflare domain and toggle the tunnel.

    Pure desired-state editor: submitting only rewrites ``entry.options``;
    the update listener in ``__init__.py`` does every side effect (sync the
    advertised URL, reconcile the add-on in a background task). A reconcile
    failure after the flow closes surfaces as a persistent notification —
    the standard HA pattern for post-flow work.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single step: domain + on/off toggle, with live add-on status."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = (user_input.get(CONF_CLOUDFLARE_DOMAIN) or "").strip()
            enabled = bool(user_input.get(CONF_TUNNEL_ENABLED, False))
            if not raw:
                if enabled:
                    errors["base"] = "domain_required"
                else:
                    # Domain cleared — the update listener retires the
                    # derived tunnel_url; tunnel management goes inert.
                    return self.async_create_entry(data={})
            else:
                domain = normalize_cloudflare_domain(raw)
                if domain is None:
                    errors[CONF_CLOUDFLARE_DOMAIN] = "invalid_domain"
                else:
                    return self.async_create_entry(
                        data={
                            CONF_CLOUDFLARE_DOMAIN: domain,
                            CONF_TUNNEL_ENABLED: enabled,
                        }
                    )

        options = self.config_entry.options
        suggested: dict[str, Any] = {
            CONF_CLOUDFLARE_DOMAIN: options.get(CONF_CLOUDFLARE_DOMAIN, ""),
            CONF_TUNNEL_ENABLED: options.get(CONF_TUNNEL_ENABLED, False),
        }
        if user_input is not None:
            # Re-showing after a validation error — keep what was typed.
            suggested.update(user_input)
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

    async def _async_tunnel_status(self) -> str:
        """Live add-on state line for the form description (best effort).

        Never raises — a wedged Supervisor must degrade to a message, not
        error the options form.
        """
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
