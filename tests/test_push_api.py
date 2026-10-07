"""View-layer tests for push_api: the push-token view's request validation.

The engines are REAL (AuthEngine and PushTokenStore over a temp HubStorage);
only ``hass`` and the request are ``view_harness`` fakes.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart.push import PushTokenStore  # noqa: E402
from casasmart.push_api import CasaSmartPushTokenView  # noqa: E402


class PushTokenBodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.push = PushTokenStore(self.rt.storage.table("push_tokens"))
        self.view = CasaSmartPushTokenView(self.hass)
        self.device_id, self.headers = H.session(self.rt.auth, role="user")

    async def test_a_json_body_that_is_not_an_object_is_a_400(self) -> None:
        # Valid JSON, wrong shape: a client bug must get the same 400 as
        # unparseable JSON, not an AttributeError (HTTP 500).
        for body in ([], ["fcm_token", "android"], "fcm-token", 42, False):
            with self.subTest(body=body):
                status, payload = H.read_response(
                    await self.view.post(H.FakeRequest(headers=self.headers, body=body))
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"message": "Invalid JSON body"})
        self.assertEqual(self.rt.push.get_all_tokens(), {})

    async def test_a_valid_body_still_registers(self) -> None:
        status, payload = H.read_response(
            await self.view.post(
                H.FakeRequest(
                    headers=self.headers,
                    body={"fcm_token": "fcm-1", "platform": "ios"},
                )
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"device_id": self.device_id, "registered": True})
        self.assertEqual(self.rt.push.get_token(self.device_id)["fcm_token"], "fcm-1")


if __name__ == "__main__":
    unittest.main()
