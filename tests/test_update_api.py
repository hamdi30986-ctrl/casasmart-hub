"""The update status check: what GitHub's answers turn into, and that a bad
answer never fails the status endpoint.

``UpdateChecker`` and the status view are real; the GitHub session is a fake
patched onto ``update_api``, and ``hass`` and the request come from
``view_harness``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart import update_api  # noqa: E402
from casasmart.const import UPDATE_REPO_CONFIG_KEY  # noqa: E402
from casasmart.update_api import (  # noqa: E402
    CasaSmartUpdateStatusView,
    UpdateChecker,
)


class _Response:
    def __init__(self, status: int, payload=None, error: Exception | None = None):
        self.status = status
        self._payload = payload
        self._error = error

    async def json(self):
        if self._error is not None:
            raise self._error
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.response


class UpdateStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.hub_config.set(UPDATE_REPO_CONFIG_KEY, "example/casasmart-hub")
        _, self.headers = H.session(self.rt.auth, role="admin")

    def _serve(self, response: _Response) -> _Session:
        session = _Session(response)
        patcher = mock.patch.object(
            update_api, "async_get_clientsession", lambda hass: session
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    async def test_a_newer_release_is_reported(self) -> None:
        session = self._serve(
            _Response(200, {"tag_name": "v9.9.9", "body": "Notes", "assets": []})
        )
        status = await UpdateChecker(self.hass, "2.3.0").async_status()
        self.assertEqual(
            session.urls,
            ["https://api.github.com/repos/example/casasmart-hub/releases/latest"],
        )
        self.assertEqual(status["latest_version"], "v9.9.9")
        self.assertTrue(status["update_available"])
        self.assertEqual(status["changelog"], "Notes")

    async def test_malformed_json_from_github_is_no_update_not_a_500(self) -> None:
        # A 200 whose body isn't JSON makes aiohttp's json() raise ValueError.
        self._serve(
            _Response(200, error=json.JSONDecodeError("Expecting value", "<", 0))
        )
        view = CasaSmartUpdateStatusView(self.hass, UpdateChecker(self.hass, "2.3.0"))
        status, body = H.read_response(
            await view.get(H.FakeRequest(headers=self.headers))
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["current_version"], "2.3.0")
        self.assertIsNone(body["latest_version"])
        self.assertFalse(body["update_available"])


if __name__ == "__main__":
    unittest.main()
