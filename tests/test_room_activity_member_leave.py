"""A member's room activity goes with their last device, on both leave paths.

Self unpair (POST /auth/unpair-self) and an admin deleting a device
(DELETE /users/{device_id}) run through the real views and AuthEngine over a
temp HubStorage; the recorder is a stand-in that records what it was asked
to forget (its own deletion is tested in test_room_activity_recorder).
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
    from casasmart.auth_api import CasaSmartUnpairSelfView, CasaSmartUserView

    _ERR = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class MemberLeaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.user_settings = types.SimpleNamespace(delete=lambda member_id: None)
        self.rt.room_activity = types.SimpleNamespace(
            async_forget_member=mock.AsyncMock()
        )
        _, self.admin_headers = H.session(self.rt.auth, role="admin")

    async def unpair_self(self, headers):
        resp = await CasaSmartUnpairSelfView(self.hass).post(
            H.FakeRequest(headers=headers)
        )
        return H.read_response(resp)[0]

    async def admin_delete(self, device_id):
        resp = await CasaSmartUserView(self.hass).delete(
            H.FakeRequest(headers=self.admin_headers), device_id
        )
        return H.read_response(resp)[0]

    async def test_self_unpair_of_the_last_device_forgets_the_member(self):
        _, headers = H.session(self.rt.auth, role="user", member_id="mem-a")
        self.assertEqual(await self.unpair_self(headers), 200)
        self.rt.room_activity.async_forget_member.assert_awaited_once_with("mem-a")

    async def test_self_unpair_with_another_device_left_keeps_the_history(self):
        H.session(self.rt.auth, role="user", member_id="mem-a")
        _, headers = H.session(self.rt.auth, role="user", member_id="mem-a")
        self.assertEqual(await self.unpair_self(headers), 200)
        self.rt.room_activity.async_forget_member.assert_not_awaited()

    async def test_admin_delete_of_the_last_device_forgets_the_member(self):
        device_id, _ = H.session(self.rt.auth, role="user", member_id="mem-b")
        self.assertEqual(await self.admin_delete(device_id), 200)
        self.rt.room_activity.async_forget_member.assert_awaited_once_with("mem-b")

    async def test_admin_delete_with_another_device_left_keeps_the_history(self):
        device_id, _ = H.session(self.rt.auth, role="user", member_id="mem-b")
        H.session(self.rt.auth, role="user", member_id="mem-b")
        self.assertEqual(await self.admin_delete(device_id), 200)
        self.rt.room_activity.async_forget_member.assert_not_awaited()

    async def test_without_a_recorder_leaving_still_works(self):
        self.rt.room_activity = None
        _, headers = H.session(self.rt.auth, role="user", member_id="mem-a")
        self.assertEqual(await self.unpair_self(headers), 200)


if __name__ == "__main__":
    unittest.main()
