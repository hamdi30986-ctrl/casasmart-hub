"""The destructive casasmart services are for Home Assistant admins only.

factory_reset unpairs every phone and set_tunnel_url changes where phones
connect, so a non-admin HA user is refused before either handler runs.
activate_scene stays open to automations and every user. Needs a real Home
Assistant (the admin check is HA's own).
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from homeassistant.exceptions import HomeAssistantError, Unauthorized

    _async_register_services = H.import_integration()._async_register_services
    from casasmart.const import DOMAIN

    _ERR = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR
_OLD_URL = "https://old.example.com"


class _Auth:
    def __init__(self, users: dict[str, bool]) -> None:
        self._users = users

    async def async_get_user(self, user_id):
        if user_id not in self._users:
            return None
        return types.SimpleNamespace(is_admin=self._users[user_id])


@unittest.skipIf(_SKIP, f"CasaSmart unimportable: {_SKIP}")
class AdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.hub_config.set("tunnel_url", _OLD_URL)
        self.rt.energy_controller = None
        self.rt.energy_flags = types.SimpleNamespace(disabled_automations=lambda: [])
        self.owner = H.enroll(self.rt.auth, role="admin")
        self.hass.auth = _Auth({"admin": True, "member": False})
        self.hass.config_entries.async_reload = mock.AsyncMock()
        self.hass.config_entries.async_update_entry = mock.Mock()
        _async_register_services(self.hass)

    async def _call(self, service: str, user_id: str | None, **data) -> None:
        handler = self.hass.services.handlers[(DOMAIN, service)]
        await handler(
            types.SimpleNamespace(
                data=data, context=types.SimpleNamespace(user_id=user_id)
            )
        )

    async def test_a_non_admin_cannot_factory_reset(self) -> None:
        with self.assertRaises(Unauthorized):
            await self._call("factory_reset", "member")
        self.assertIsNotNone(self.rt.auth.get_device(self.owner))
        self.hass.config_entries.async_reload.assert_not_called()

    async def test_a_non_admin_cannot_set_the_tunnel_url(self) -> None:
        with self.assertRaises(Unauthorized):
            await self._call("set_tunnel_url", "member", url="https://evil.example.net")
        self.assertEqual(self.rt.hub_config.get("tunnel_url"), _OLD_URL)

    async def test_admins_and_automations_still_can(self) -> None:
        for user_id in ("admin", None):
            with self.subTest(user_id=user_id):
                await self._call(
                    "set_tunnel_url", user_id, url="https://new.example.com"
                )
                self.assertEqual(
                    self.rt.hub_config.get("tunnel_url"), "https://new.example.com"
                )
        await self._call("factory_reset", "admin")
        self.assertIsNone(self.rt.auth.get_device(self.owner))
        self.hass.config_entries.async_reload.assert_awaited_once()

    async def test_a_failed_reload_is_reported_and_keeps_wiped_phones_out(
        self,
    ) -> None:
        # Token checks answer from the engine's cache, which only a reload
        # used to rebuild, so a reload that failed left the wiped phones
        # trusted and the service reporting success.
        token = H.token_for(self.rt.auth, self.owner)
        self.hass.config_entries.async_reload = mock.AsyncMock(return_value=False)
        with self.assertRaisesRegex(HomeAssistantError, "restart Home Assistant"):
            await self._call("factory_reset", "admin")
        self.assertIsNone(self.rt.auth.get_device(self.owner))
        with self.assertRaises(Exception):
            self.rt.auth.validate_token(token)

    async def test_any_user_can_run_a_scene(self) -> None:
        # The member gets past the admin gate to the handler's own refusal.
        with self.assertRaisesRegex(Exception, "Unknown scene"):
            await self._call("activate_scene", "member", scene_id="scene-missing")


if __name__ == "__main__":
    unittest.main()
