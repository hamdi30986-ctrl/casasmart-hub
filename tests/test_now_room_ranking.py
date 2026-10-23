"""The Now page's room ranking through the real view.

GET /api/casasmart/now with the real registry, NowDataEngine, suggestion
runtime, filtering and ranking over a temp HubStorage; only hass, the request
and Home Assistant's entity registry are fakes (as in
test_now_room_activity_golden). Real-HA environment only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.now_api import CasaSmartNowView
    from casasmart.now_data import NowDataEngine
    from casasmart.suggestion_runtime import SuggestionRuntime
    from casasmart.suggestion_store import SuggestionStore
    from homeassistant.helpers import entity_registry as er

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


class _NoEntityRegistry:
    """HA's entity registry with no entries: every entity is visible."""

    def async_get(self, entity_id):
        return None


ROOMS = {
    "room-majlis": {"name": "Majlis", "floor_id": None, "icon": None, "sort_order": 0},
    "room-kitchen": {
        "name": "Kitchen",
        "floor_id": None,
        "icon": None,
        "sort_order": 1,
    },
    "room-bedroom": {
        "name": "Bedroom",
        "floor_id": None,
        "icon": None,
        "sort_order": 2,
    },
    "room-office": {"name": "Office", "floor_id": None, "icon": None, "sort_order": 3},
    "room-guest": {"name": "Guest", "floor_id": None, "icon": None, "sort_order": 4},
    "room-hall": {"name": "Hall", "floor_id": None, "icon": None, "sort_order": 5},
}

AC = {"hvac_modes": ["off", "cool", "heat"]}


POWER_W = {"device_class": "power", "unit_of_measurement": "W"}
POWER_KW = {"device_class": "power", "unit_of_measurement": "kW"}


# device id -> (room, [(entity_id, state, attributes)]): every device has its
# own meter, so the home is fully metered.
DEVICES = {
    "dev-majlis-ac": (
        "room-majlis",
        [
            ("climate.majlis", "cool", AC),
            ("sensor.majlis_ac_power", "2.1", POWER_KW),
        ],
    ),
    "dev-kitchen": (
        "room-kitchen",
        [
            ("light.kitchen_1", "on", {}),
            ("light.kitchen_2", "on", {}),
            ("sensor.kitchen_power", "45", POWER_W),
        ],
    ),
    "dev-bedroom": (
        "room-bedroom",
        [
            ("light.bedroom", "off", {}),
            ("sensor.bedroom_power", "0", POWER_W),
        ],
    ),
    "dev-office": ("room-office", [("light.office", "off", {})]),
    "dev-guest": ("room-guest", [("light.guest", "off", {})]),
    "dev-hall": ("room-hall", [("light.hall", "off", {})]),
}

# The fields every room had before the ranking, in their order.
LEGACY_ROOM_KEYS = [
    "room_id",
    "name",
    "floor_id",
    "icon",
    "sort_order",
    "active_count",
    "eligible_entity_ids",
    "restore_pending_count",
    "most_recent_activity_at",
]


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class NowRoomRankingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self.tmp.name)
        storage = self.runtime.storage
        self.addCleanup(storage.close)
        self.hass.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        for room_id, record in ROOMS.items():
            storage.table("registry_rooms")[room_id] = record
        for device_id, (room_id, entities) in DEVICES.items():
            for eid, state, attributes in entities:
                storage.table("registry_devices")[eid] = {
                    "room_id": room_id,
                    "display_name": None,
                    "sort_order": 0,
                }
                self.hass.states.add(eid, state=state, attributes=dict(attributes))
            storage.table("registry_user_devices")[device_id] = {
                "entity_ids": [eid for eid, _, _ in entities]
            }
        self.runtime.registry.warm_up()
        self.now_data = NowDataEngine(
            storage.table("now_recents"),
            storage.table("now_room_policies"),
            storage.table("now_config"),
            storage.table("now_restore_sets"),
            storage.table("now_idempotency"),
        )
        self.runtime.now_data = self.now_data
        self.runtime.energy = None
        self.runtime.suggestions = SuggestionRuntime(
            self.hass,
            SuggestionStore(storage),
            self.runtime.registry,
            now_data=self.now_data,
        )
        self.view = CasaSmartNowView(self.hass)

    async def room_activity(self, *, rooms=None, role="admin"):
        _, headers = H.session(self.runtime.auth, role=role, rooms=rooms)
        status, body = H.read_response(
            await self.view.get(H.FakeRequest(headers=headers))
        )
        self.assertEqual(status, 200)
        return body["room_activity"]

    async def test_a_fully_metered_home_ranks_by_watts_and_fills_the_slots(self):
        activity = await self.room_activity()
        self.assertEqual(
            list(activity),
            ["rooms", "layout", "configured_room_count", "ranking"],
        )
        self.assertEqual(
            activity["ranking"],
            {"version": 1, "basis": "watts", "slots": 5, "mode": "on"},
        )
        rooms = activity["rooms"]
        # The AC-only Majlis (2.1 kW) beats the Kitchen's two lights (45 W);
        # three off rooms fill the slots in the room order.
        self.assertEqual(
            [(r["room_id"], r["rank"]["filler"]) for r in rooms],
            [
                ("room-majlis", False),
                ("room-kitchen", False),
                ("room-bedroom", True),
                ("room-office", True),
                ("room-guest", True),
            ],
        )
        self.assertEqual(
            json.dumps(rooms[0]),
            json.dumps(
                {
                    "room_id": "room-majlis",
                    **ROOMS["room-majlis"],
                    "active_count": 0,
                    "eligible_entity_ids": [],
                    "restore_pending_count": 0,
                    "most_recent_activity_at": None,
                    "rank": {
                        "active": True,
                        "filler": False,
                        "on_count": 1,
                        "ac_on_count": 1,
                        "watts": 2100.0,
                        "watts_quality": "complete",
                        "reason": "active",
                        "params": {},
                    },
                }
            ),
        )
        self.assertEqual(
            (rooms[2]["rank"]["watts"], rooms[3]["rank"]["watts"]), (0.0, None)
        )
        # Five rooms: the first is featured, as for five rooms that are on.
        self.assertEqual(
            activity["layout"],
            {
                "featured_room_id": "room-majlis",
                "cards": ["room-kitchen", "room-bedroom", "room-office", "room-guest"],
                "view_all_count": 0,
            },
        )

    async def test_every_room_keeps_its_old_fields_and_adds_rank_last(self):
        for room in (await self.room_activity())["rooms"]:
            self.assertEqual(list(room), [*LEGACY_ROOM_KEYS, "rank"])

    async def test_an_unmetered_light_switches_the_whole_list_to_count(self):
        self.hass.states.add("light.majlis_lamp", state="on")
        self.runtime.storage.table("registry_devices")["light.majlis_lamp"] = {
            "room_id": "room-majlis",
            "display_name": None,
            "sort_order": 0,
        }
        self.runtime.storage.table("registry_user_devices")["dev-lamp"] = {
            "entity_ids": ["light.majlis_lamp"]
        }
        self.runtime.registry.warm_up()
        activity = await self.room_activity()
        self.assertEqual(activity["ranking"]["basis"], "count")
        # Majlis and Kitchen have two on each; Majlis has the lower sort_order.
        self.assertEqual(
            [r["room_id"] for r in activity["rooms"][:2]],
            ["room-majlis", "room-kitchen"],
        )
        self.assertEqual(activity["rooms"][0]["rank"]["watts_quality"], "partial")

    async def test_a_scoped_member_is_ranked_on_their_rooms_only(self):
        self.hass.states.get("sensor.majlis_ac_power").state = "unavailable"
        admin = await self.room_activity()
        self.assertEqual(admin["ranking"]["basis"], "count")
        member = await self.room_activity(
            rooms=["room-kitchen", "room-office"], role="user"
        )
        self.assertEqual(member["ranking"]["basis"], "watts")
        self.assertEqual(
            [(r["room_id"], r["rank"]["filler"]) for r in member["rooms"]],
            [("room-kitchen", False), ("room-office", True)],
        )

    async def test_nothing_on_fills_every_slot(self):
        for eid in ("climate.majlis", "light.kitchen_1", "light.kitchen_2"):
            self.hass.states.get(eid).state = "off"
        activity = await self.room_activity()
        self.assertEqual(
            [r["room_id"] for r in activity["rooms"]],
            [
                "room-majlis",
                "room-kitchen",
                "room-bedroom",
                "room-office",
                "room-guest",
            ],
        )
        self.assertTrue(all(r["rank"]["filler"] for r in activity["rooms"]))
        self.assertEqual(activity["ranking"]["basis"], "count")


if __name__ == "__main__":
    unittest.main()
