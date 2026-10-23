"""A member's room activity goes with their last device, on both leave paths.

Self unpair (POST /auth/unpair-self) and an admin deleting a device
(DELETE /users/{device_id}) run through the real views and AuthEngine over a
temp HubStorage; the recorder and the habit learner are stand-ins that
record what they were asked to forget (their own deletion is tested in
test_room_activity_recorder and test_room_habit_learner, the whole path in
test_room_habits_ha). The member's recent devices (now_recents) go too.
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
        self.forgotten = []

        def forgetter(name):
            async def forget(member_id):
                self.forgotten.append((name, member_id))

            return mock.AsyncMock(side_effect=forget)

        self.rt.room_activity = types.SimpleNamespace(
            async_forget_member=forgetter("activity")
        )
        self.rt.room_habits = types.SimpleNamespace(
            async_forget_member=forgetter("habits")
        )
        for member_id in ("mem-a", "mem-b", "mem-keep"):
            self.rt.now_data.record_successful_control(member_id, "light.majlis")
            self.rt.now_data.save_idempotent_result(
                member_id, "room-majlis", "turn_off", "key-12345678", {"ok": True}
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

    def recents(self):
        """Each member's recent devices; their stored room-command answers
        must go with them."""
        recents = {
            member: [item["entity_id"] for item in self.rt.now_data.recents_for(member)]
            for member in ("mem-a", "mem-b", "mem-keep")
        }
        for member, items in recents.items():
            stored = self.rt.now_data.idempotent_result(
                member, "room-majlis", "turn_off", "key-12345678"
            )
            self.assertEqual(stored is not None, bool(items), member)
        return recents

    async def test_self_unpair_of_the_last_device_forgets_the_member(self):
        _, headers = H.session(self.rt.auth, role="user", member_id="mem-a")
        self.assertEqual(await self.unpair_self(headers), 200)
        # The log rows first, then what was learned from them.
        self.assertEqual(self.forgotten, [("activity", "mem-a"), ("habits", "mem-a")])
        self.assertEqual(
            self.recents(),
            {"mem-a": [], "mem-b": ["light.majlis"], "mem-keep": ["light.majlis"]},
        )

    async def test_self_unpair_with_another_device_left_keeps_the_history(self):
        H.session(self.rt.auth, role="user", member_id="mem-a")
        _, headers = H.session(self.rt.auth, role="user", member_id="mem-a")
        self.assertEqual(await self.unpair_self(headers), 200)
        self.assertEqual(self.forgotten, [])
        self.assertEqual(self.recents()["mem-a"], ["light.majlis"])

    async def test_admin_delete_of_the_last_device_forgets_the_member(self):
        device_id, _ = H.session(self.rt.auth, role="user", member_id="mem-b")
        self.assertEqual(await self.admin_delete(device_id), 200)
        self.assertEqual(self.forgotten, [("activity", "mem-b"), ("habits", "mem-b")])
        self.assertEqual(
            self.recents(),
            {"mem-a": ["light.majlis"], "mem-b": [], "mem-keep": ["light.majlis"]},
        )

    async def test_admin_delete_with_another_device_left_keeps_the_history(self):
        device_id, _ = H.session(self.rt.auth, role="user", member_id="mem-b")
        H.session(self.rt.auth, role="user", member_id="mem-b")
        self.assertEqual(await self.admin_delete(device_id), 200)
        self.assertEqual(self.forgotten, [])
        self.assertEqual(self.recents()["mem-b"], ["light.majlis"])

    async def test_without_a_recorder_leaving_still_works(self):
        self.rt.room_activity = None
        _, headers = H.session(self.rt.auth, role="user", member_id="mem-a")
        self.assertEqual(await self.unpair_self(headers), 200)
        self.assertEqual(self.forgotten, [("habits", "mem-a")])

    async def test_without_a_learner_leaving_still_works(self):
        self.rt.room_habits = None
        device_id, _ = H.session(self.rt.auth, role="user", member_id="mem-b")
        self.assertEqual(await self.admin_delete(device_id), 200)
        self.assertEqual(self.forgotten, [("activity", "mem-b")])
        self.assertEqual(self.recents()["mem-b"], [])


if __name__ == "__main__":
    unittest.main()
