"""View-layer tests for the unauthenticated handshake in ``api``.

Runs where Home Assistant is importable (the view harness needs it).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hastubs import install_casasmart_package, install_homeassistant_stubs

# Install the stubs (a no-op where a real Home Assistant is importable) before
# the harness imports the package.
install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402

try:
    from casasmart.api import CasaSmartHandshakeView

    _ERR = None
except Exception as err:
    CasaSmartHandshakeView = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class HandshakeHubNameTests(unittest.IsolatedAsyncioTestCase):
    """The phone's first-pair scanner labels each hub it finds on the LAN."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.tls = None

    async def _hub_name(self, **request):
        request.setdefault("remote", "192.168.1.20")
        view = CasaSmartHandshakeView(self.hass, "2.3.0")
        status, body = H.read_response(await view.get(H.FakeRequest(**request)))
        self.assertEqual(status, 200)
        return body.get("hub_name")

    async def test_configured_name_is_reported(self) -> None:
        self.rt.hub_config.set("hub_name", "  Beach House ")
        self.assertEqual(await self._hub_name(), "Beach House")

    async def test_default_name_without_a_usable_one(self) -> None:
        self.assertEqual(await self._hub_name(), "CasaSmart Hub")
        for unusable in ("   ", 42):
            with self.subTest(hub_name=unusable):
                self.rt.hub_config.set("hub_name", unusable)
                self.assertEqual(await self._hub_name(), "CasaSmart Hub")

    async def test_default_name_while_the_hub_is_loading(self) -> None:
        self.hass.config_entries.async_loaded_entries = lambda domain: []
        self.assertEqual(await self._hub_name(), "CasaSmart Hub")

    async def test_name_is_left_out_off_the_lan(self) -> None:
        # The handshake is unauthenticated and reachable through the tunnel.
        self.rt.hub_config.set("hub_name", "Beach House")
        for request in (
            {"remote": "127.0.0.1"},
            {"remote": "93.184.216.34"},
            {"remote": "172.17.0.2", "headers": {"CF-Ray": "8a1b2c3d4e5f6789-AMS"}},
        ):
            with self.subTest(**request):
                self.assertIsNone(await self._hub_name(**request))


if __name__ == "__main__":
    unittest.main()
