"""Strict relay URL, migration, and non-secret reload-state tests."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package

install_casasmart_package()

from casasmart.const import (  # noqa: E402
    CONF_CLOUDFLARE_DOMAIN,
    CONF_PUSH_RELAY_URL,
    CONF_RELAY_ACTIVATION_CODE,
    CONF_RELAY_ACTIVATION_REQUEST_ID,
)
from casasmart.relay_config import (  # noqa: E402
    async_reload_relay_runtime,
    migrate_relay_options,
    normalize_relay_base_url,
    quiesce_relay_runtime,
    relay_config_snapshot,
    relay_endpoints,
    relay_reload_required,
    without_relay_activation,
)

ACTIVATION = f"CSACT1.{'a' * 40}.{'b' * 86}"


# 1.7.0 removed the built-in default relay; tests use an example origin.
PUSH_RELAY_URL_DEFAULT = "https://relay.example.com"


class RelayUrlValidationTests(unittest.TestCase):
    def test_normalizes_valid_production_origins(self) -> None:
        cases = {
            "https://relay.example.com": "https://relay.example.com",
            " HTTPS://RELAY.EXAMPLE.COM/ ": "https://relay.example.com",
            "https://relay.example.com:443": "https://relay.example.com",
            "https://relay.example.com:8443/": "https://relay.example.com:8443",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_relay_base_url(raw), expected)

    def test_rejects_non_production_or_ambiguous_urls(self) -> None:
        rejected = [
            None,
            443,
            "",
            "relay.example.com",
            "http://relay.example.com",
            "https://user@relay.example.com",
            "https://user:pass@relay.example.com",
            "https://relay.example.com/push",
            "https://relay.example.com//",
            "https://relay.example.com?debug=1",
            "https://relay.example.com#fragment",
            "https://relay.example.com\\@attacker.example",
            "https://relay.example.com\n",
            "https://relay.exa mple.com",
            "https://127.0.0.1",
            "https://[::1]",
            "https://localhost",
            "https://relay.local",
            "https://relay.internal",
            "https://relay.example.com:99999",
            "https://relay.example.com:0",
            "https://relay.example.com:abc",
            "https://réseau.example.com",
            "https://-relay.example.com",
            "https://relay-.example.com",
        ]
        for raw in rejected:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_relay_base_url(raw))


class RelayMigrationTests(unittest.TestCase):
    def test_valid_option_precedes_legacy_and_preserves_other_options(self) -> None:
        result = migrate_relay_options(
            {
                CONF_PUSH_RELAY_URL: "https://option.example.com/",
                CONF_CLOUDFLARE_DOMAIN: "home.example.com",
            },
            "https://legacy.example.com",
        )
        self.assertEqual(result.base_url, "https://option.example.com")
        self.assertEqual(result.options[CONF_CLOUDFLARE_DOMAIN], "home.example.com")
        self.assertTrue(result.legacy_present)
        self.assertTrue(result.legacy_valid)

    def test_v162_legacy_override_is_preserved_and_normalized(self) -> None:
        result = migrate_relay_options({}, "https://fleet.example.com/")
        self.assertEqual(result.base_url, "https://fleet.example.com")
        self.assertEqual(
            result.options[CONF_PUSH_RELAY_URL], "https://fleet.example.com"
        )

    def test_v162_without_override_leaves_relay_unset(self) -> None:
        # 1.7.0 removed the built-in default: no override means no relay until
        # the installer configures one (relay activation is part of setup).
        result = migrate_relay_options({}, None)
        self.assertIsNone(result.base_url)
        self.assertNotIn(CONF_PUSH_RELAY_URL, result.options)
        self.assertFalse(result.legacy_present)

    def test_insecure_legacy_override_is_not_migrated(self) -> None:
        result = migrate_relay_options({}, "http://127.0.0.1:8000")
        self.assertIsNone(result.base_url)
        self.assertNotIn(CONF_PUSH_RELAY_URL, result.options)
        self.assertTrue(result.legacy_present)
        self.assertFalse(result.legacy_valid)


class RelayReloadStateTests(unittest.TestCase):
    def test_server_change_rebuilds_both_endpoints_from_same_base(self) -> None:
        applied = relay_config_snapshot(
            {CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT}, {}
        )
        requested = relay_config_snapshot(
            {CONF_PUSH_RELAY_URL: "https://new-relay.example.com"},
            {
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION,
                CONF_RELAY_ACTIVATION_REQUEST_ID: "request-2",
            },
        )
        self.assertTrue(relay_reload_required(applied, requested))
        endpoints = relay_endpoints(requested.base_url)
        self.assertEqual(endpoints.base_url, "https://new-relay.example.com")
        self.assertEqual(endpoints.push_url, "https://new-relay.example.com/push")
        self.assertEqual(
            endpoints.registration_url,
            "https://new-relay.example.com/register-hub",
        )

    def test_same_server_fresh_activation_triggers_recovery_reload(self) -> None:
        applied = relay_config_snapshot(
            {CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT}, {}
        )
        requested = relay_config_snapshot(
            {CONF_PUSH_RELAY_URL: PUSH_RELAY_URL_DEFAULT},
            {
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION,
                CONF_RELAY_ACTIVATION_REQUEST_ID: "request-1",
            },
        )
        self.assertTrue(relay_reload_required(applied, requested))

    def test_successful_ack_deletes_credential_and_marker_only(self) -> None:
        cleaned = without_relay_activation(
            {
                "other": "keep",
                CONF_RELAY_ACTIVATION_CODE: ACTIVATION,
                CONF_RELAY_ACTIVATION_REQUEST_ID: "request-1",
            }
        )
        self.assertEqual(cleaned, {"other": "keep"})
        self.assertNotIn(ACTIVATION, repr(cleaned))

    def test_quiesce_stops_all_old_relay_components(self) -> None:
        class _Stopper:
            def __init__(self) -> None:
                self.stopped = False

            def async_stop(self) -> None:
                self.stopped = True

            def stop(self) -> None:
                self.stopped = True

        monitor, dispatcher, registrar = _Stopper(), _Stopper(), _Stopper()
        runtime = types.SimpleNamespace(
            tank_push_monitor=monitor,
            push_dispatcher=dispatcher,
            relay_registrar=registrar,
        )
        quiesce_relay_runtime(runtime)
        self.assertTrue(monitor.stopped)
        self.assertTrue(dispatcher.stopped)
        self.assertTrue(registrar.stopped)
        self.assertIsNone(runtime.tank_push_monitor)
        self.assertIsNone(runtime.push_dispatcher)
        self.assertIsNone(runtime.relay_registrar)


class RelayReloadFailureTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _entry():
        class _Stopper:
            def async_stop(self) -> None:
                pass

            def stop(self) -> None:
                pass

        return types.SimpleNamespace(
            entry_id="entry-1",
            runtime_data=types.SimpleNamespace(
                tank_push_monitor=_Stopper(),
                push_dispatcher=_Stopper(),
                relay_registrar=_Stopper(),
            ),
        )

    async def test_reload_false_leaves_old_relay_runtime_quiesced(self) -> None:
        entry = self._entry()

        class _Entries:
            async def async_reload(self, entry_id):
                self.entry_id = entry_id
                return False

        entries = _Entries()
        result = await async_reload_relay_runtime(
            types.SimpleNamespace(config_entries=entries), entry
        )
        self.assertFalse(result)
        self.assertEqual(entries.entry_id, "entry-1")
        self.assertIsNone(entry.runtime_data.tank_push_monitor)
        self.assertIsNone(entry.runtime_data.push_dispatcher)
        self.assertIsNone(entry.runtime_data.relay_registrar)

    async def test_reload_exception_is_contained_and_reports_failure(self) -> None:
        entry = self._entry()

        class _Entries:
            async def async_reload(self, entry_id):
                raise RuntimeError("reload failed")

        result = await async_reload_relay_runtime(
            types.SimpleNamespace(config_entries=_Entries()), entry
        )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
