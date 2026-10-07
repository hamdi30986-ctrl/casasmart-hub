"""View-layer tests for the installer config-flow proxy in ``admin_api``.

Runs where Home Assistant is importable (the view harness needs it).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hastubs import install_casasmart_package, install_homeassistant_stubs

# Collected before the other admin suite: install the stubs (a no-op where a
# real Home Assistant is importable) before the harness imports the package.
install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402

try:
    from casasmart.admin_api import CasaSmartAdminConfigFlowsView

    _ERR = None
except Exception as err:
    CasaSmartAdminConfigFlowsView = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class ConfigFlowInitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.view = CasaSmartAdminConfigFlowsView(self.hass)

    async def test_non_string_handler_is_400(self) -> None:
        # A list/object handler is unhashable: the whitelist check must reject
        # it as input, not raise TypeError (a 500).
        _, hdr = H.session(self.rt.auth, role="admin")
        for handler in (["broadlink"], {"name": "broadlink"}):
            resp = await self.view.post(
                H.FakeRequest(headers=hdr, body={"handler": handler})
            )
            status, _ = H.read_response(resp)
            self.assertEqual(status, 400)

    async def test_unlisted_handler_is_400(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        resp = await self.view.post(
            H.FakeRequest(headers=hdr, body={"handler": "mqtt"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
