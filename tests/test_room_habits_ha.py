"""Room habits through the real views, over the real log and learner.

GET /api/casasmart/now through the real CasaSmartNowView, with the real
registry, NowDataEngine, room activity table, recorder (only read through)
and HabitLearner over a temp HubStorage; Home Assistant's entity registry,
hass and the requests are the view harness' fakes. History is written into
the room activity table as the recorder would have logged it over the past
eight weeks (the recorder's own attribution is pinned in
test_room_activity_recorder). Real-HA environment only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.now_api import CasaSmartNowView
    from casasmart.now_data import NowDataEngine
    from casasmart.room_intel.habits import local_day
    from casasmart.room_intel.learner import HabitLearner
    from casasmart.room_intel.recorder import ActivityRecorder
    from homeassistant.helpers import entity_registry as er

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err

RIYADH = 180
ROOMS = {
    "room-majlis": {"name": "Majlis", "floor_id": None, "icon": None, "sort_order": 0},
    "room-kitchen": {"name": "Kitchen", "floor_id": None, "icon": None, "sort_order": 1},
    "room-bedroom": {"name": "Bedroom", "floor_id": None, "icon": None, "sort_order": 2},
    "room-office": {"name": "Office", "floor_id": None, "icon": None, "sort_order": 3},
}  # fmt: skip
DEVICES = {
    "dev-majlis": ("room-majlis", ["light.majlis"]),
    "dev-kitchen": ("room-kitchen", ["light.kitchen"]),
    "dev-bedroom": ("room-bedroom", ["light.bedroom"]),
    "dev-office": ("room-office", ["light.office"]),
}


class _NoEntityRegistry:
    def async_get(self, entity_id):
        return None


class HabitHubCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self.tmp.name)
        storage = self.runtime.storage
        self.storage = storage
        self.addCleanup(storage.close)
        self.hass.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        for room_id, record in ROOMS.items():
            storage.table("registry_rooms")[room_id] = record
        for device_id, (room_id, entities) in DEVICES.items():
            for eid in entities:
                storage.table("registry_devices")[eid] = {
                    "room_id": room_id,
                    "display_name": None,
                    "sort_order": 0,
                }
                self.hass.states.add(eid, state="off")
            storage.table("registry_user_devices")[device_id] = {"entity_ids": entities}
        self.runtime.registry.warm_up()
        self.runtime.now_data = NowDataEngine(
            storage.table("now_recents"),
            storage.table("now_room_policies"),
            storage.table("now_config"),
            storage.table("now_restore_sets"),
            storage.table("now_idempotency"),
        )
        self.runtime.energy = None
        self.runtime.suggestions = None
        self.runtime.room_activity = ActivityRecorder(
            self.hass, self.runtime.registry, storage.room_activity()
        )
        self.runtime.room_habits = HabitLearner(
            self.hass,
            storage.table("now_room_habits"),
            storage.table("now_habit_config"),
            storage.room_activity(),
            recorder=self.runtime.room_activity,
        )
        await self.runtime.room_habits.async_start()
        self.history()

    def history(self):
        """Eight weeks, at this hour and the hours either side of it: someone
        in Home Assistant switches the majlis on (household only); member A
        switches the bedroom on from the app; the kitchen gets a wall press
        at another time of day."""
        now = time.time()
        today = local_day(now, RIYADH)
        hour = int((now + RIYADH * 60) // 3600) % 24
        events = []
        for day in range(today - 56, today):
            for h in (hour - 1, hour, hour + 1):
                t = day * 86400 + (h % 24) * 3600 + 600 - RIYADH * 60
                events.append(self.event("room-majlis", "light.majlis", t, "ha_user"))
                events.append(
                    self.event("room-bedroom", "light.bedroom", t, "app", "mem-a")
                )
            t = day * 86400 + ((hour + 12) % 24) * 3600 - RIYADH * 60
            events.append(self.event("room-kitchen", "light.kitchen", t))
        self.storage.room_activity().append_many(events)

    @staticmethod
    def event(room_id, entity_id, t, source="wall_switch", member_id=None):
        return {
            "t": int(t),
            "utc_offset_min": RIYADH,
            "room_id": room_id,
            "entity_id": entity_id,
            "action": "on",
            "source": source,
            "member_id": member_id,
        }

    async def now_page(self, *, member_id, rooms=None, role="admin", query=None):
        _, headers = H.session(
            self.runtime.auth, role=role, rooms=rooms, member_id=member_id
        )
        return H.read_response(
            await CasaSmartNowView(self.hass).get(
                H.FakeRequest(headers=headers, query=query)
            )
        )

    async def room_activity(self, **kwargs):
        status, body = await self.now_page(**kwargs)
        self.assertEqual(status, 200)
        return body["room_activity"]

    @staticmethod
    def reasons(block):
        return [(r["room_id"], r["rank"]["reason"]) for r in block["rooms"]]


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class NowPageHabits(HabitHubCase):
    async def test_the_tablet_gets_the_households_habits(self):
        block = await self.room_activity(
            member_id="mem-a", query={"habits": "household"}
        )
        self.assertEqual(
            block["ranking"]["habits"], {"layer": "household", "status": "learning"}
        )
        self.assertEqual(
            self.reasons(block),
            [
                ("room-majlis", "habit_now"),
                ("room-bedroom", "habit_now"),
                ("room-kitchen", "room_order"),
                ("room-office", "room_order"),
            ],
        )
        self.assertEqual(block["rooms"][0]["rank"]["params"], {})

    async def test_a_phone_gets_its_members_habits_over_the_households(self):
        # Member A's own taps are the bedroom: the majlis (others' habit)
        # drops back for them.
        block = await self.room_activity(member_id="mem-a")
        self.assertEqual(
            block["ranking"]["habits"], {"layer": "personal", "status": "learning"}
        )
        self.assertEqual(
            self.reasons(block),
            [
                ("room-bedroom", "habit_now"),
                ("room-majlis", "room_order"),
                ("room-kitchen", "room_order"),
                ("room-office", "room_order"),
            ],
        )
        # Someone with no app history gets the household's.
        block = await self.room_activity(member_id="mem-b")
        self.assertEqual(
            [r for r, reason in self.reasons(block) if reason == "habit_now"],
            ["room-majlis", "room-bedroom"],
        )
        self.assertNotIn("mem-", json.dumps(block))
        self.assertNotIn("light.", json.dumps([r["rank"] for r in block["rooms"]]))

    async def test_a_bad_habits_value_is_a_400(self):
        status, body = await self.now_page(
            member_id="mem-a", query={"habits": "everyone"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "habits must be household or personal")

    async def test_a_scoped_member_gets_only_their_rooms_habits(self):
        block = await self.room_activity(
            member_id="mem-c", rooms=["room-kitchen", "room-office"], role="user"
        )
        self.assertEqual(
            self.reasons(block),
            [("room-kitchen", "room_order"), ("room-office", "room_order")],
        )
        # The kitchen's wall presses are enough history to rank on.
        self.assertEqual(block["ranking"]["habits"]["status"], "learning")

    async def test_learning_off_ranks_without_habits(self):
        await self.runtime.room_habits.async_update({"learning": False}, managed_ids=())
        block = await self.room_activity(member_id="mem-b")
        self.assertEqual(
            block["ranking"]["habits"], {"layer": "personal", "status": "off"}
        )
        self.assertNotIn("habit_now", json.dumps(block))

    async def test_the_model_is_read_from_the_log_once(self):
        for member in ("mem-a", "mem-b", "mem-c"):
            await self.room_activity(member_id=member)
        await self.room_activity(member_id="mem-a", query={"habits": "household"})
        self.assertEqual(self.runtime.room_habits.builds, 1)


if __name__ == "__main__":
    unittest.main()
