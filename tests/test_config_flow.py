"""Config-flow tests — Phase 2 of the pairing redesign (cloud stays on).

Pins the fresh-install seeding contract of ``config_flow.py``:

* Providing a Cloudflare domain at setup seeds ``tunnel_enabled: True`` —
  the reconciler in ``__init__.py`` reads options with a False fallback, so
  the key must be PRESENT and True or a fresh install would still stop the
  add-on. This is the Phase 2 change (was: seed False = auto-disable).
* No domain / invalid domain behave exactly as before.
* The gear-icon options flow KEEPS the on/off toggle as a manual emergency
  switch: submitting OFF persists OFF, ON persists ON, and the toggle is
  still part of the options schema. Phase 2 removes only the automatic
  disable, never the manual one.

Harness note: Home Assistant's real ``ConfigFlow``/``OptionsFlow`` can only
run under the flow *manager* (``async_create_entry`` dereferences
``self.flow_id``), which no suite here spins up — so this module always
imports ``casasmart.config_flow`` against a minimal stand-in
``homeassistant.config_entries`` (plus ``exceptions``/``aiohttp_client``
shims for the ``tunnel_control`` import chain where the shared stub package
lacks them). Every module this suite adds to ``sys.modules`` is removed
again right after the import, so sibling suites — including the
container-only view suites and their skip guards — see the exact same
environment whether or not this suite ran first (the ``hastubs`` doctrine).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()


# --------------------------------------------------------------------------- #
# Temporary import environment (see module docstring)
# --------------------------------------------------------------------------- #
def _flow_stub_module() -> types.ModuleType:
    """A ``homeassistant.config_entries`` stand-in for manager-less flows."""
    mod = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:  # attribute bag is all config_flow.py touches
        def __init__(self, options=None) -> None:
            self.options = dict(options or {})

    class _FlowBase:
        """The two FlowHandler seams config_flow.py calls."""

        def async_create_entry(self, *, title=None, data=None, options=None):
            return {
                "type": "create_entry",
                "title": title,
                "data": data,
                "options": options,
            }

        def async_show_form(
            self,
            *,
            step_id,
            data_schema=None,
            errors=None,
            description_placeholders=None,
        ):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders,
            }

        def add_suggested_values_to_schema(self, data_schema, suggested_values):
            return data_schema

    class ConfigFlow(_FlowBase):
        def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
            super().__init_subclass__(**kwargs)
            cls._domain = domain

    class OptionsFlow(_FlowBase):
        pass

    mod.ConfigEntry = ConfigEntry
    mod.ConfigFlow = ConfigFlow
    mod.ConfigFlowResult = dict
    mod.OptionsFlow = OptionsFlow
    return mod


def _import_config_flow():
    """Import ``casasmart.config_flow`` hermetically; restore sys.modules."""
    added: list[tuple[str, object | None]] = []  # (name, previous module)

    def _shim(name: str, mod: types.ModuleType) -> None:
        added.append((name, sys.modules.get(name)))
        sys.modules[name] = mod
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, mod)

    # exceptions / aiohttp_client: real ones exist in the container; the
    # shared stub package deliberately omits them (skip-guard doctrine), so
    # shim only where the import fails.
    for name, attrs in (
        ("homeassistant.exceptions", {"HomeAssistantError": type("HomeAssistantError", (Exception,), {})}),
        ("homeassistant.helpers.aiohttp_client", {"async_get_clientsession": lambda hass: None}),
    ):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 — stubbed local env
            mod = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(mod, key, value)
            _shim(name, mod)

    # config_entries: ALWAYS the stand-in — manager-less flow driving (see
    # module docstring), identical behavior locally and in the container.
    _shim("homeassistant.config_entries", _flow_stub_module())

    try:
        return importlib.import_module("casasmart.config_flow")
    finally:
        for name, previous in reversed(added):
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if previous is None:
                sys.modules.pop(name, None)
                if parent is not None and getattr(parent, child, None) is not None:
                    delattr(parent, child)
            else:
                sys.modules[name] = previous
                if parent is not None:
                    setattr(parent, child, previous)


config_flow = _import_config_flow()

from casasmart.const import (  # noqa: E402
    CONF_CLOUDFLARE_DOMAIN,
    CONF_TUNNEL_ENABLED,
)

DOMAIN_IN = "my-ha.example.com"


def _fake_hass():
    """Enough hass for CloudflaredController.available() to answer False."""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(components=set()), data={}
    )


def _user_flow():
    flow = config_flow.CasaSmartConfigFlow()
    flow.hass = _fake_hass()
    return flow


def _options_flow(options=None):
    flow = config_flow.CasaSmartOptionsFlow()
    flow.hass = _fake_hass()
    flow.config_entry = types.SimpleNamespace(options=dict(options or {}))
    return flow


# --------------------------------------------------------------------------- #
# Fresh install (async_step_user) — the Phase 2 contract
# --------------------------------------------------------------------------- #
class FreshInstallSeeding(unittest.IsolatedAsyncioTestCase):
    async def test_domain_seeds_tunnel_enabled_true(self) -> None:
        """THE Phase 2 assertion: cloud stays on at fresh install.

        The key must be PRESENT and True — the reconciler falls back to
        False on an absent key and would stop the add-on.
        """
        result = await _user_flow().async_step_user(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN}
        )
        self.assertEqual(result["type"], "create_entry")
        options = result["options"]
        self.assertEqual(options[CONF_CLOUDFLARE_DOMAIN], DOMAIN_IN)
        self.assertIn(CONF_TUNNEL_ENABLED, options)
        self.assertIs(options[CONF_TUNNEL_ENABLED], True)

    async def test_pasted_https_url_normalized_and_stays_on(self) -> None:
        result = await _user_flow().async_step_user(
            {CONF_CLOUDFLARE_DOMAIN: f"https://{DOMAIN_IN}/"}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["options"][CONF_CLOUDFLARE_DOMAIN], DOMAIN_IN)
        self.assertIs(result["options"][CONF_TUNNEL_ENABLED], True)

    async def test_no_domain_unchanged(self) -> None:
        """Tunnel-less install: entry created with no options at all."""
        result = await _user_flow().async_step_user({CONF_CLOUDFLARE_DOMAIN: ""})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {})
        self.assertIsNone(result["options"])

    async def test_invalid_domain_still_rejected(self) -> None:
        result = await _user_flow().async_step_user(
            {CONF_CLOUDFLARE_DOMAIN: "not a domain!"}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"], {CONF_CLOUDFLARE_DOMAIN: "invalid_domain"}
        )


# --------------------------------------------------------------------------- #
# Options flow — the manual emergency switch survives Phase 2
# --------------------------------------------------------------------------- #
class OptionsFlowKeepsManualSwitch(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_still_in_options_schema(self) -> None:
        keys = [marker.schema for marker in config_flow.OPTIONS_SCHEMA.schema]
        self.assertIn(CONF_TUNNEL_ENABLED, keys)

    async def test_manual_off_persists(self) -> None:
        """The emergency cut-off: an explicit OFF is stored as OFF."""
        result = await _options_flow(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: True}
        ).async_step_init(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: False}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: False},
        )

    async def test_manual_on_persists(self) -> None:
        result = await _options_flow(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: False}
        ).async_step_init(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: True}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: True},
        )

    async def test_enabled_without_domain_still_errors(self) -> None:
        result = await _options_flow(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: True}
        ).async_step_init({CONF_CLOUDFLARE_DOMAIN: "", CONF_TUNNEL_ENABLED: True})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "domain_required"})

    async def test_clearing_domain_still_retires_options(self) -> None:
        result = await _options_flow(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: True}
        ).async_step_init({CONF_CLOUDFLARE_DOMAIN: "", CONF_TUNNEL_ENABLED: False})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {})

    async def test_form_shows_without_supervisor(self) -> None:
        """First open (no input): form renders, status degrades gracefully."""
        result = await _options_flow(
            {CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN, CONF_TUNNEL_ENABLED: True}
        ).async_step_init(None)
        self.assertEqual(result["type"], "form")
        self.assertIn(
            "tunnel_status", result["description_placeholders"] or {}
        )


if __name__ == "__main__":
    unittest.main()
