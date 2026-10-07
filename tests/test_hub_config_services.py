"""Service handlers that save hub settings report a failed save cleanly.

A ConfigError (say, a read-only data directory) must reach Home Assistant as
a HomeAssistantError: the caller sees the reason, and the log no traceback.
A factory reset that fails part-way leaves no hub that is half wiped, or
wiped with live caches.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hastubs import install_casasmart_package, install_homeassistant_stubs

# Install the stubs (a no-op where a real Home Assistant is importable) before
# the harness imports the package.
install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from homeassistant.exceptions import HomeAssistantError

    _async_register_services = H.import_integration()._async_register_services
    from casasmart.const import DOMAIN
    from casasmart.storage import JsonConfigStore, config_store, store

    _ERR = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


def _public_pem() -> str:
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@unittest.skipIf(_SKIP, f"CasaSmart unimportable: {_SKIP}")
class HubConfigSaveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        # A real config file with every key the handlers write or delete.
        self.rt.hub_config = JsonConfigStore(Path(self._tmp.name) / "hub_config.json")
        self.rt.hub_config.update(
            {
                "tunnel_url": "https://old.example.com",
                "registry_imported": True,
                "bootstrap_code_hash": "x",
                "recovery_code_hash": "y",
            }
        )
        self.before = self.rt.hub_config.as_dict()
        self.rt.energy_controller = None
        self.rt.energy_flags = types.SimpleNamespace(disabled_automations=lambda: [])
        self.hass.auth = types.SimpleNamespace(
            async_get_user=self._admin_user,
        )
        self.hass.config_entries.async_reload = mock.AsyncMock()
        _async_register_services(self.hass)

    @staticmethod
    async def _admin_user(user_id):
        return types.SimpleNamespace(is_admin=True)

    def _saves_fail(self):
        # What a read-only data directory does to the atomic write.
        return mock.patch.object(
            config_store.tempfile,
            "mkstemp",
            side_effect=PermissionError(13, "Permission denied"),
        )

    async def _call(self, service: str, **data) -> None:
        handler = self.hass.services.handlers[(DOMAIN, service)]
        call = types.SimpleNamespace(
            data=data, context=types.SimpleNamespace(user_id="admin")
        )
        await handler(call)

    async def test_set_tunnel_url(self) -> None:
        with self._saves_fail(), self.assertRaises(HomeAssistantError):
            await self._call("set_tunnel_url", url="https://new.example.com")
        self.assertEqual(self.rt.hub_config.as_dict(), self.before)

    async def test_configure_hq_notifications(self) -> None:
        with self._saves_fail(), self.assertRaises(HomeAssistantError):
            await self._call("configure_hq_notifications", public_key=_public_pem())
        self.assertEqual(self.rt.hub_config.as_dict(), self.before)

    async def test_factory_reset(self) -> None:
        with self._saves_fail(), self.assertRaises(HomeAssistantError):
            await self._call("factory_reset")
        self.assertEqual(self.rt.hub_config.as_dict(), self.before)
        # The tables were wiped before the save failed, so the in-memory
        # caches must be rebuilt from them.
        self.hass.config_entries.async_reload.assert_awaited_once()

    async def test_factory_reset_wipes_all_tables_or_none(self) -> None:
        owner = H.enroll(self.rt.auth, role="admin")
        self.rt.storage.table("user_settings")["member"] = {"theme": "dark"}
        real_clear = store.KeyValueTable.clear
        cleared = []

        def _fifth_clear_fails(table) -> None:
            cleared.append(table)
            if len(cleared) == 5:
                raise sqlite3.OperationalError("disk I/O error")
            real_clear(table)

        with (
            mock.patch.object(store.KeyValueTable, "clear", _fifth_clear_fails),
            self.assertRaises(HomeAssistantError),
        ):
            await self._call("factory_reset")
        self.assertIsNotNone(self.rt.auth.get_device(owner))
        self.assertEqual(
            self.rt.storage.table("user_settings").get("member"), {"theme": "dark"}
        )
        self.assertEqual(self.rt.hub_config.as_dict(), self.before)


if __name__ == "__main__":
    unittest.main()
