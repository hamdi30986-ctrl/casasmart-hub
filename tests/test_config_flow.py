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
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()


# --------------------------------------------------------------------------- #
# Temporary import environment (see module docstring)
# --------------------------------------------------------------------------- #
def _flow_stub_module() -> types.ModuleType:
    """A ``homeassistant.config_entries`` stand-in for manager-less flows."""
    mod = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:  # attribute bag is all config_flow.py touches
        def __init__(self, options=None, data=None, runtime_data=None) -> None:
            self.options = dict(options or {})
            self.data = dict(data or {})
            self.runtime_data = runtime_data

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
            self.last_suggested_values = dict(suggested_values or {})
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
        (
            "homeassistant.exceptions",
            {"HomeAssistantError": type("HomeAssistantError", (Exception,), {})},
        ),
        (
            "homeassistant.helpers.aiohttp_client",
            {"async_get_clientsession": lambda hass: None},
        ),
    ):
        try:
            importlib.import_module(name)
        except Exception:
            mod = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(mod, key, value)
            _shim(name, mod)

    # config_entries: ALWAYS the stand-in — manager-less flow driving (see
    # module docstring), identical behavior locally and in the container.
    _shim("homeassistant.config_entries", _flow_stub_module())

    selector_mod = types.ModuleType("homeassistant.helpers.selector")

    class TextSelectorType:
        PASSWORD = "password"

    @dataclass
    class TextSelectorConfig:
        type: str
        autocomplete: str | None = None

    class TextSelector:
        def __init__(self, config: TextSelectorConfig) -> None:
            self.config = config

        def __call__(self, value):
            return value

    selector_mod.TextSelectorType = TextSelectorType
    selector_mod.TextSelectorConfig = TextSelectorConfig
    selector_mod.TextSelector = TextSelector
    _shim("homeassistant.helpers.selector", selector_mod)

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
    CONF_PUSH_RELAY_URL,
    CONF_RELAY_ACTIVATION_CODE,
    CONF_RELAY_ACTIVATION_REQUEST_ID,
    CONF_TUNNEL_ENABLED,
    PUSH_RELAY_URL_CONFIG_KEY,
)

DOMAIN_IN = "my-ha.example.com"
ACTIVATION_IN = f"CSACT1.{'a' * 40}.{'b' * 86}"


# 1.7.0 removed the built-in default relay; tests use an example origin.
PUSH_RELAY_URL_DEFAULT = "https://relay.example.com"


class _ConfigEntriesManager:
    def async_update_entry(self, entry, *, data=None, options=None, **kwargs):
        changed = False
        if data is not None and entry.data != data:
            entry.data = dict(data)
            changed = True
        if options is not None and entry.options != options:
            entry.options = dict(options)
            changed = True
        return changed


def _fake_hass():
    """Enough hass for CloudflaredController.available() to answer False."""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(components=set()),
        config_entries=_ConfigEntriesManager(),
        data={},
    )


def _user_flow():
    flow = config_flow.CasaSmartConfigFlow()
    flow.hass = _fake_hass()
    return flow


def _options_flow(options=None, data=None, runtime_data=None):
    flow = config_flow.CasaSmartOptionsFlow()
    flow.hass = _fake_hass()
    flow.config_entry = types.SimpleNamespace(
        options=dict(options or {}),
        data=dict(data or {}),
        runtime_data=runtime_data,
    )
    return flow


def _options_input(
    domain: str = DOMAIN_IN,
    enabled: bool = True,
    relay: str = PUSH_RELAY_URL_DEFAULT,
    activation: str | None = None,
) -> dict:
    values = {
        CONF_PUSH_RELAY_URL: relay,
        CONF_CLOUDFLARE_DOMAIN: domain,
        CONF_TUNNEL_ENABLED: enabled,
    }
    if activation is not None:
        values[CONF_RELAY_ACTIVATION_CODE] = activation
    return values


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
            {
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN,
            }
        )
        self.assertEqual(result["type"], "create_entry")
        options = result["options"]
        self.assertEqual(options[CONF_CLOUDFLARE_DOMAIN], DOMAIN_IN)
        self.assertIn(CONF_TUNNEL_ENABLED, options)
        self.assertIs(options[CONF_TUNNEL_ENABLED], True)
        self.assertEqual(result["data"], {CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN})

    async def test_pasted_https_url_normalized_and_stays_on(self) -> None:
        result = await _user_flow().async_step_user(
            {
                CONF_CLOUDFLARE_DOMAIN: f"https://{DOMAIN_IN}/",
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN,
            }
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["options"][CONF_CLOUDFLARE_DOMAIN], DOMAIN_IN)
        self.assertIs(result["options"][CONF_TUNNEL_ENABLED], True)

    async def test_no_domain_records_the_entered_relay(self) -> None:
        """Tunnel-less install records the relay the installer entered."""
        result = await _user_flow().async_step_user(
            {
                CONF_CLOUDFLARE_DOMAIN: "",
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN,
            }
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN})
        self.assertEqual(
            result["options"], {CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT}
        )

    async def test_invalid_domain_still_rejected(self) -> None:
        result = await _user_flow().async_step_user(
            {
                CONF_CLOUDFLARE_DOMAIN: "not a domain!",
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN,
            }
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {CONF_CLOUDFLARE_DOMAIN: "invalid_domain"})

    async def test_activation_code_is_required_and_shape_checked(self) -> None:
        result = await _user_flow().async_step_user(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_RELAY_ACTIVATION_CODE: "",
            }
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"],
            {CONF_RELAY_ACTIVATION_CODE: "invalid_activation_code"},
        )

    async def test_relay_url_is_required(self) -> None:
        # 1.7.0 removed the built-in default relay: setup must name one.
        result = await _user_flow().async_step_user(
            {
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN,
            }
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {CONF_PUSH_RELAY_URL: "invalid_relay_url"})

    async def test_relay_help_text_gets_its_example_url(self) -> None:
        # strings.json keeps URLs out of translations ({example_url}); every
        # form that shows the relay field must supply the placeholder.
        result = await _user_flow().async_step_user(None)
        self.assertEqual(
            result["description_placeholders"]["example_url"],
            "https://relay.example.com",
        )
        strings = (
            Path(__file__).resolve().parents[1]
            / "custom_components/casasmart/strings.json"
        ).read_text()
        self.assertNotRegex(strings, r"https://[\w-]+\.[\w.-]+")  # no literal URLs

    async def test_activation_schema_uses_password_selector(self) -> None:
        marker = next(
            marker
            for marker in config_flow.STEP_USER_SCHEMA.schema
            if marker.schema == CONF_RELAY_ACTIVATION_CODE
        )
        selector_value = config_flow.STEP_USER_SCHEMA.schema[marker]
        self.assertEqual(selector_value.config.type, "password")
        self.assertEqual(selector_value.config.autocomplete, "off")


# --------------------------------------------------------------------------- #
# Options flow — the manual emergency switch survives Phase 2
# --------------------------------------------------------------------------- #
class OptionsFlowKeepsManualSwitch(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_still_in_options_schema(self) -> None:
        keys = [marker.schema for marker in config_flow.OPTIONS_SCHEMA.schema]
        self.assertIn(CONF_TUNNEL_ENABLED, keys)

    async def test_relay_and_masked_activation_are_in_options_schema(self) -> None:
        keys = [marker.schema for marker in config_flow.OPTIONS_SCHEMA.schema]
        self.assertIn(CONF_PUSH_RELAY_URL, keys)
        marker = next(
            marker
            for marker in config_flow.OPTIONS_SCHEMA.schema
            if marker.schema == CONF_RELAY_ACTIVATION_CODE
        )
        selector_value = config_flow.OPTIONS_SCHEMA.schema[marker]
        self.assertEqual(selector_value.config.type, "password")
        self.assertEqual(selector_value.config.autocomplete, "off")

    async def test_manual_off_persists(self) -> None:
        """The emergency cut-off: an explicit OFF is stored as OFF."""
        result = await _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: True,
            }
        ).async_step_init(_options_input(enabled=False))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: False,
            },
        )

    async def test_manual_on_persists(self) -> None:
        result = await _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: False,
            }
        ).async_step_init(_options_input(enabled=True))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: True,
            },
        )

    async def test_enabled_without_domain_still_errors(self) -> None:
        result = await _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: True,
            }
        ).async_step_init(_options_input(domain="", enabled=True))
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "domain_required"})

    async def test_clearing_domain_still_retires_options(self) -> None:
        result = await _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: True,
            }
        ).async_step_init(_options_input(domain="", enabled=False))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT})

    async def test_form_shows_without_supervisor(self) -> None:
        """First open (no input): form renders, status degrades gracefully."""
        result = await _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: True,
            }
        ).async_step_init(None)
        self.assertEqual(result["type"], "form")
        self.assertIn("tunnel_status", result["description_placeholders"] or {})


class RelayOptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    class _Stopper:
        def __init__(self) -> None:
            self.stopped = False

        def async_stop(self) -> None:
            self.stopped = True

        def stop(self) -> None:
            self.stopped = True

    @staticmethod
    def _runtime(legacy=None):
        class _HubConfig:
            def get(self, key):
                if key == PUSH_RELAY_URL_CONFIG_KEY:
                    return legacy
                return None

        return types.SimpleNamespace(
            hub_config=_HubConfig(),
            relay_config_applied=None,
            tank_push_monitor=RelayOptionsFlowTests._Stopper(),
            push_dispatcher=RelayOptionsFlowTests._Stopper(),
            relay_registrar=RelayOptionsFlowTests._Stopper(),
        )

    async def test_existing_options_display_current_effective_relay(self) -> None:
        flow = _options_flow({CONF_PUSH_RELAY_URL: "https://fleet.example.com"})
        result = await flow.async_step_init(None)
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            flow.last_suggested_values[CONF_PUSH_RELAY_URL],
            "https://fleet.example.com",
        )
        self.assertNotIn(CONF_RELAY_ACTIVATION_CODE, flow.last_suggested_values)

    async def test_unmigrated_legacy_relay_is_displayed_without_switching(self) -> None:
        flow = _options_flow(
            {}, runtime_data=self._runtime("https://legacy.example.com/")
        )
        await flow.async_step_init(None)
        self.assertEqual(
            flow.last_suggested_values[CONF_PUSH_RELAY_URL],
            "https://legacy.example.com",
        )

    async def test_unset_existing_entry_displays_an_empty_relay(self) -> None:
        # No built-in default since 1.7.0: an unconfigured entry shows an empty
        # field rather than inventing a relay.
        flow = _options_flow({})
        await flow.async_step_init(None)
        self.assertEqual(flow.last_suggested_values[CONF_PUSH_RELAY_URL], "")

    async def test_server_change_requires_complete_activation_code(self) -> None:
        result = await _options_flow(
            {CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT}
        ).async_step_init(
            _options_input(
                domain="",
                enabled=False,
                relay="https://new-relay.example.com",
            )
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"],
            {CONF_RELAY_ACTIVATION_CODE: "activation_required"},
        )

    async def test_server_change_commits_url_and_secret_data_atomically(self) -> None:
        runtime = self._runtime()
        flow = _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: True,
            },
            runtime_data=runtime,
        )
        result = await flow.async_step_init(
            _options_input(
                relay="https://new-relay.example.com/",
                activation=ACTIVATION_IN,
            )
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            flow.config_entry.options[CONF_PUSH_RELAY_URL],
            "https://new-relay.example.com",
        )
        self.assertEqual(
            flow.config_entry.data[CONF_RELAY_ACTIVATION_CODE], ACTIVATION_IN
        )
        self.assertIn(CONF_RELAY_ACTIVATION_REQUEST_ID, flow.config_entry.data)
        self.assertNotIn(CONF_RELAY_ACTIVATION_CODE, flow.config_entry.options)
        self.assertNotIn(ACTIVATION_IN, repr(result))
        self.assertIsNone(runtime.tank_push_monitor)
        self.assertIsNone(runtime.push_dispatcher)
        self.assertIsNone(runtime.relay_registrar)

    async def test_same_server_activation_recovers_without_url_change(self) -> None:
        runtime = self._runtime()
        flow = _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: False,
            },
            runtime_data=runtime,
        )
        result = await flow.async_step_init(
            _options_input(enabled=False, activation=ACTIVATION_IN)
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            flow.config_entry.options[CONF_PUSH_RELAY_URL],
            PUSH_RELAY_URL_DEFAULT,
        )
        self.assertEqual(
            flow.config_entry.data[CONF_RELAY_ACTIVATION_CODE], ACTIVATION_IN
        )
        self.assertNotIn(ACTIVATION_IN, repr(result))

    async def test_recovery_preserves_cloudflare_options(self) -> None:
        flow = _options_flow(
            {
                CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT,
                CONF_CLOUDFLARE_DOMAIN: DOMAIN_IN,
                CONF_TUNNEL_ENABLED: False,
            },
            runtime_data=self._runtime(),
        )
        result = await flow.async_step_init(
            _options_input(enabled=False, activation=ACTIVATION_IN)
        )
        self.assertEqual(result["data"][CONF_CLOUDFLARE_DOMAIN], DOMAIN_IN)
        self.assertIs(result["data"][CONF_TUNNEL_ENABLED], False)

    async def test_invalid_secret_is_never_echoed_or_stored_in_options(self) -> None:
        malformed = "CSACT1.do-not-echo"
        flow = _options_flow({CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT})
        result = await flow.async_step_init(
            _options_input(domain="", enabled=False, activation=malformed)
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"],
            {CONF_RELAY_ACTIVATION_CODE: "invalid_activation_code"},
        )
        self.assertNotIn(malformed, repr(result))
        self.assertNotIn(malformed, repr(flow.last_suggested_values))
        self.assertNotIn(CONF_RELAY_ACTIVATION_CODE, flow.config_entry.options)

    async def test_setup_error_does_not_echo_valid_activation(self) -> None:
        flow = _user_flow()
        result = await flow.async_step_user(
            {
                CONF_CLOUDFLARE_DOMAIN: "not a domain!",
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION_IN,
            }
        )
        self.assertEqual(result["type"], "form")
        self.assertNotIn(ACTIVATION_IN, repr(result))
        self.assertNotIn(ACTIVATION_IN, repr(flow.last_suggested_values))


if __name__ == "__main__":
    unittest.main()
