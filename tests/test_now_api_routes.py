"""Which HTTP methods the room-activity views register.

The views are registered the way both listeners register them
(``HomeAssistantView.register`` on an aiohttp router), and requests are
resolved against that router, so the test sees the real 405 a client would.

Container/CI only (imports Home Assistant).
"""

from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from casasmart import now_api

    _ERR = None
except Exception as err:
    now_api = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class RoomActivityRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        hass = types.SimpleNamespace(data={})
        self.app = web.Application()
        for view in (
            now_api.CasaSmartRoomActivityPolicyView(hass),
            now_api.CasaSmartRoomActivityCommandView(hass),
        ):
            view.register(hass, self.app, self.app.router)

    async def status(self, method: str, path: str) -> int | None:
        """The router's refusal status, or None when a handler matches."""
        request = make_mocked_request(method, path, app=self.app)
        match = await self.app.router.resolve(request)
        exception = match.http_exception
        return None if exception is None else exception.status

    async def test_policy_is_set_only_on_its_own_path(self) -> None:
        room = "/api/casasmart/now/rooms/room-1"
        self.assertIsNone(await self.status("PUT", f"{room}/activity-policy"))
        self.assertIsNone(await self.status("POST", f"{room}/activity"))
        self.assertEqual(await self.status("PUT", f"{room}/activity"), 405)


if __name__ == "__main__":
    unittest.main()
