"""Golden pin of the Now page's room_activity block, byte for byte.

GET /api/casasmart/now through the real view, with the real registry,
NowDataEngine, suggestion runtime and filtering over a temp HubStorage; only
hass, the request and Home Assistant's entity registry are fakes. The block is
compared as JSON text, so the room order, every field and the key order are
all pinned. Real-HA environment only, like the other view-layer suites.

The ranking changed the answers on purpose: ties go to the room order (sort_order,
name, room id), off rooms fill the five card slots, and every room carries a
rank block, the block a ranking summary.

Each room then gained its rank reason and params, and the summary's mode. The
hub here has no room activity log, no meter and no media, so off rooms stay
in the room order.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime

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
    "room-a": {"name": "Majlis", "floor_id": None, "icon": "sofa", "sort_order": 2},
    "room-b": {"name": "Kitchen", "floor_id": None, "icon": None, "sort_order": 0},
    "room-c": {"name": "Bedroom", "floor_id": None, "icon": "bed", "sort_order": 1},
    "room-e": {"name": "Anteroom", "floor_id": None, "icon": None, "sort_order": 0},
}

# (entity_id, state, room, attributes), in Home Assistant's state order.
STATES = [
    ("light.a_ceiling", "on", "room-a", {}),
    ("switch.a_outlet", "on", "room-a", {"device_class": "outlet"}),
    ("fan.a_fan", "off", "room-a", {}),
    ("switch.b_heater", "on", "room-b", {"device_class": "switch"}),
    ("light.b_spot", "on", "room-b", {}),
    ("switch.b_relay_left", "on", "room-b", {"device_class": "switch"}),
    ("light.c_lamp", "off", "room-c", {}),
    ("light.e_desk", "on", "room-e", {}),
    ("fan.e_fan", "on", "room-e", {}),
    ("light.e_unimported", "on", "room-e", {}),
]

USER_DEVICES = {
    "dev-a": {
        "entity_ids": ["light.a_ceiling", "switch.a_outlet", "fan.a_fan"],
        "gangs": {"switch.a_outlet": {"type": "outlet"}},
    },
    "dev-b": {
        "entity_ids": ["switch.b_heater", "light.b_spot", "switch.b_relay_left"],
        "gangs": {"switch.b_heater": {"type": "heater"}},
        "gang_types": {"left": "light"},
    },
    "dev-c": {"entity_ids": ["light.c_lamp"]},
    "dev-e": {"entity_ids": ["light.e_desk", "fan.e_fan"]},
}


def room(
    room_id, active_count, eligible_entity_ids, restore_pending_count=0, *, filler=False
):
    """A room as the Now page sends it (this fixture has no AC and no meter)."""
    return {
        "room_id": room_id,
        **ROOMS[room_id],
        "active_count": active_count,
        "eligible_entity_ids": eligible_entity_ids,
        "restore_pending_count": restore_pending_count,
        "most_recent_activity_at": None,
        "rank": {
            "active": active_count > 0,
            "filler": filler,
            "on_count": active_count,
            "ac_on_count": 0,
            "watts": None,
            "watts_quality": "absent",
            "reason": "active" if active_count > 0 else "room_order",
            "params": {},
        },
    }


RANKING = {"version": 1, "basis": "count", "slots": 5, "mode": "on"}
# Nothing in the caller's rooms is on.
RANKING_ALL_OFF = {**RANKING, "mode": "all_off"}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class NowRoomActivityGoldenTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self.tmp.name)
        storage = self.runtime.storage
        self.addCleanup(storage.close)
        self.hass.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        for room_id, record in ROOMS.items():
            storage.table("registry_rooms")[room_id] = record
        for eid, state, room_id, attributes in STATES:
            storage.table("registry_devices")[eid] = {
                "room_id": room_id,
                "display_name": None,
                "sort_order": 0,
            }
            self.hass.states.add(eid, state=state, attributes=dict(attributes))
            self.hass.states.get(eid).last_changed = datetime(
                2026, 10, 23, 9, 0, tzinfo=UTC
            )
        for device_id, record in USER_DEVICES.items():
            storage.table("registry_user_devices")[device_id] = record
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
        return json.dumps(body["room_activity"])

    async def test_without_policies(self):
        self.assertEqual(
            await self.room_activity(),
            json.dumps(
                {
                    # A three-way tie in the room order (Anteroom and
                    # Kitchen have sort_order 0, Majlis 2), then Bedroom
                    # fills a slot.
                    "rooms": [
                        room("room-e", 2, ["light.e_desk", "fan.e_fan"]),
                        room("room-b", 2, ["light.b_spot", "switch.b_relay_left"]),
                        room(
                            "room-a",
                            2,
                            ["light.a_ceiling", "switch.a_outlet", "fan.a_fan"],
                        ),
                        room("room-c", 0, ["light.c_lamp"], filler=True),
                    ],
                    "layout": {
                        "featured_room_id": None,
                        "cards": ["room-e", "room-b", "room-a", "room-c"],
                        "view_all_count": 0,
                    },
                    "configured_room_count": 0,
                    "ranking": RANKING,
                }
            ),
        )

    async def test_with_policies_and_a_capture(self):
        self.now_data.set_room_policy(
            "room-a", True, ["light.a_ceiling", "switch.a_outlet", "fan.a_fan"]
        )
        self.now_data.set_room_policy(
            "room-b", True, ["light.b_spot", "switch.b_relay_left"]
        )
        self.now_data.set_room_policy("room-c", True, ["light.c_lamp"])
        self.now_data.set_room_policy("room-e", False, ["light.e_desk"])
        self.now_data.save_restore_set("room-c", ["light.c_lamp"])
        # A capture on a room that no longer takes part is not reported.
        self.now_data.save_restore_set("room-e", ["light.e_desk"])
        self.assertEqual(
            await self.room_activity(),
            json.dumps(
                {
                    "rooms": [
                        room("room-b", 2, ["light.b_spot", "switch.b_relay_left"]),
                        room("room-a", 1, ["light.a_ceiling", "fan.a_fan"]),
                        room("room-c", 0, ["light.c_lamp"], 1, filler=True),
                    ],
                    "layout": {
                        "featured_room_id": None,
                        "cards": ["room-b", "room-a", "room-c"],
                        "view_all_count": 0,
                    },
                    "configured_room_count": 3,
                    "ranking": RANKING,
                }
            ),
        )

    async def test_scoped_member(self):
        self.now_data.set_room_policy("room-c", True, ["light.c_lamp"])
        self.now_data.save_restore_set("room-c", ["light.c_lamp"])
        self.assertEqual(
            await self.room_activity(rooms=["room-c", "room-e"], role="user"),
            json.dumps(
                {
                    "rooms": [room("room-c", 0, ["light.c_lamp"], 1, filler=True)],
                    "layout": {
                        "featured_room_id": None,
                        "cards": ["room-c"],
                        "view_all_count": 0,
                    },
                    "configured_room_count": 1,
                    "ranking": RANKING_ALL_OFF,
                }
            ),
        )

    async def test_more_than_four_rooms_feature_the_first(self):
        for index in range(4):
            room_id = f"room-z{index}"
            self.runtime.storage.table("registry_rooms")[room_id] = {
                "name": f"Z{index}",
                "floor_id": None,
                "icon": None,
                "sort_order": 0,
            }
            eid = f"light.z{index}"
            self.runtime.storage.table("registry_devices")[eid] = {
                "room_id": room_id,
                "display_name": None,
                "sort_order": 0,
            }
            self.runtime.storage.table("registry_user_devices")[f"dev-z{index}"] = {
                "entity_ids": [eid]
            }
            self.hass.states.add(eid, state="on")
        self.runtime.registry.warm_up()
        body = json.loads(await self.room_activity())
        # Seven rooms are on, so no off room fills a slot.
        self.assertEqual(
            [r["room_id"] for r in body["rooms"]],
            ["room-e", "room-b", "room-a", "room-z0", "room-z1", "room-z2", "room-z3"],
        )
        self.assertEqual(
            body["layout"],
            {
                "featured_room_id": "room-e",
                "cards": ["room-b", "room-a", "room-z0", "room-z1"],
                "view_all_count": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()
