"""Golden pins for the room ranking the Now page and generated suggestions share.

The expected values are written out by hand, field by field and in key order,
so a refactor of the ranking has to reproduce today's answers exactly: the
order, the tie-break by room id (not name, not sort_order), every field of
every room, the order of eligible_entity_ids, and the states the generated
plans are built from. Runs without Home Assistant, on the suggestion-runtime
fixture test_generated_suggestions uses.
"""

import json
import unittest
from types import SimpleNamespace as NS

import test_suggestion_api as api_fixture

RUNTIME = api_fixture.RUNTIME

# Registry rooms, deliberately not in id order.
ROOMS = [
    {
        "room_id": "room-e",
        "name": "Anteroom",
        "floor_id": "f2",
        "icon": None,
        "sort_order": 0,
    },
    {
        "room_id": "room-c",
        "name": "Bedroom",
        "floor_id": None,
        "icon": "bed",
        "sort_order": 1,
    },
    {
        "room_id": "room-a",
        "name": "Majlis",
        "floor_id": "f1",
        "icon": "sofa",
        "sort_order": 2,
    },
    {
        "room_id": "room-d",
        "name": "Empty",
        "floor_id": None,
        "icon": None,
        "sort_order": 0,
    },
    {
        "room_id": "room-b",
        "name": "Kitchen",
        "floor_id": "f1",
        "icon": None,
        "sort_order": 0,
    },
]

# (entity_id, state, room, attributes), in Home Assistant's state order.
STATES = [
    ("light.a_ceiling", "on", "room-a", {}),
    ("switch.a_outlet", "on", "room-a", {"device_class": "outlet"}),
    ("fan.a_fan", "off", "room-a", {}),
    (
        "light.a_excluded",
        "on",
        "room-a",
        {"casasmart_room_activity_exclude": True},
    ),
    ("light.a_unimported", "on", "room-a", {}),
    ("light.a_config", "on", "room-a", {}),
    ("switch.b_heater", "on", "room-b", {"device_class": "switch"}),
    ("light.b_spot", "on", "room-b", {}),
    ("switch.b_relay_left", "on", "room-b", {"device_class": "switch"}),
    ("light.c_hidden", "on", "room-c", {}),
    ("light.c_lamp", "unavailable", "room-c", {}),
    ("climate.c_ac", "cool", "room-c", {"hvac_modes": ["off", "cool"]}),
    ("light.e_desk", "on", "room-e", {}),
    ("switch.e_plain", "on", "room-e", {"device_class": "switch"}),
    ("light.e_strip", "unknown", "room-e", {}),
    ("fan.e_fan", "on", "room-e", {}),
    ("light.x_ghost", "on", "room-x", {}),
    ("light.nowhere", "on", None, {}),
    ("light.d_lamp", "off", "room-d", {}),
]

USER_DEVICES = [
    {
        "control_entity_ids": [
            "light.a_ceiling",
            "switch.a_outlet",
            "fan.a_fan",
            "light.a_excluded",
            "light.a_config",
        ],
        "config_entity_ids": ["light.a_config"],
        "gangs": {"switch.a_outlet": {"type": "outlet"}},
    },
    {
        "control_entity_ids": ["switch.b_heater", "light.b_spot"],
        "gangs": {"switch.b_heater": {"type": "heater"}},
    },
    # An older record: entity_ids instead of control_entity_ids, and a gang
    # type keyed by the channel suffix.
    {"entity_ids": ["switch.b_relay_left"], "gang_types": {"left": "light"}},
    {
        "control_entity_ids": ["light.c_hidden", "light.c_lamp", "climate.c_ac"],
        "gangs": {"light.c_hidden": {"presentation": "hidden"}},
    },
    {
        "control_entity_ids": [
            "light.e_desk",
            "switch.e_plain",
            "light.e_strip",
            "fan.e_fan",
        ]
    },
    {"control_entity_ids": ["light.x_ghost", "light.nowhere", "light.d_lamp"]},
]

POLICIES = {
    "room-a": {
        "participates": True,
        "eligible_entity_ids": [
            "light.a_ceiling",
            "switch.a_outlet",
            "fan.a_fan",
            "light.a_excluded",
        ],
    },
    "room-b": {
        "participates": True,
        "eligible_entity_ids": [
            "light.b_spot",
            "switch.b_relay_left",
            "switch.b_heater",
        ],
    },
    "room-c": {"participates": True, "eligible_entity_ids": []},
    "room-d": {"participates": False, "eligible_entity_ids": ["light.d_lamp"]},
}

SERVED_CONTROLS = [
    "light.a_ceiling",
    "switch.a_outlet",
    "fan.a_fan",
    "light.a_excluded",
    "switch.b_heater",
    "light.b_spot",
    "switch.b_relay_left",
    "light.c_lamp",
    "climate.c_ac",
    "light.e_desk",
    "switch.e_plain",
    "light.e_strip",
    "fan.e_fan",
    "light.x_ghost",
    "light.nowhere",
    "light.d_lamp",
]


def room(room_id, active_count, eligible_entity_ids):
    """An expected ranked room: the registry record, then the ranking's fields."""
    record = next(r for r in ROOMS if r["room_id"] == room_id)
    return {
        **record,
        "active_count": active_count,
        "eligible_entity_ids": eligible_entity_ids,
        "restore_pending_count": 0,
        "most_recent_activity_at": None,
    }


class RoomRankingGoldenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        values = {
            eid: NS(entity_id=eid, state=state, attributes=dict(attributes))
            for eid, state, _, attributes in STATES
        }
        self.states = NS(values=values, async_all=lambda: list(values.values()))
        self.states.get = values.get

        async def executor(fn, *args):
            return fn(*args)

        self.hass = NS(
            states=self.states,
            rooms={eid: rid for eid, _, rid, _ in STATES},
            config=NS(time_zone="UTC"),
            async_add_executor_job=executor,
        )
        self.policies = {}
        self.registry = NS(
            list_rooms=lambda: [dict(r) for r in ROOMS],
            list_user_devices=lambda: [dict(d) for d in USER_DEVICES],
        )
        now_data = NS(
            room_policy=lambda rid: self.policies.get(
                rid, {"participates": False, "eligible_entity_ids": []}
            )
        )
        self.runtime = RUNTIME.GeneratedSuggestionRuntime(
            self.hass, None, self.registry, now_data
        )

    async def rank(self, scope=None):
        ranked, grouped, states = await self.runtime.room_context(scope)
        return (
            json.dumps(ranked),
            {rid: [s.entity_id for s in items] for rid, items in grouped.items()},
            list(states),
        )

    async def test_without_policies_every_visible_room_ranks_by_count_then_id(self):
        ranked, grouped, states = await self.rank()
        self.assertEqual(
            ranked,
            json.dumps(
                [
                    room(
                        "room-a",
                        3,
                        [
                            "light.a_ceiling",
                            "switch.a_outlet",
                            "fan.a_fan",
                            "light.a_excluded",
                        ],
                    ),
                    # room-b beats room-e on id although "Anteroom" sorts first
                    # by name; sort_order plays no part.
                    room("room-b", 2, ["light.b_spot", "switch.b_relay_left"]),
                    room("room-e", 2, ["light.e_desk", "fan.e_fan"]),
                ]
            ),
        )
        self.assertEqual(
            grouped,
            {
                "room-e": [
                    "light.e_desk",
                    "switch.e_plain",
                    "light.e_strip",
                    "fan.e_fan",
                ],
                "room-c": ["light.c_lamp", "climate.c_ac"],
                "room-a": [
                    "light.a_ceiling",
                    "switch.a_outlet",
                    "fan.a_fan",
                    "light.a_excluded",
                ],
                "room-d": ["light.d_lamp"],
                "room-b": ["switch.b_heater", "light.b_spot", "switch.b_relay_left"],
            },
        )
        self.assertEqual(states, SERVED_CONTROLS)

    async def test_scope_is_applied_before_ranking(self):
        ranked, grouped, states = await self.rank(["room-b", "room-c", "room-e"])
        self.assertEqual(
            ranked,
            json.dumps(
                [
                    room("room-b", 2, ["light.b_spot", "switch.b_relay_left"]),
                    room("room-e", 2, ["light.e_desk", "fan.e_fan"]),
                ]
            ),
        )
        self.assertEqual(
            grouped,
            {
                "room-e": [
                    "light.e_desk",
                    "switch.e_plain",
                    "light.e_strip",
                    "fan.e_fan",
                ],
                "room-c": ["light.c_lamp", "climate.c_ac"],
                "room-b": ["switch.b_heater", "light.b_spot", "switch.b_relay_left"],
            },
        )
        self.assertEqual(
            states,
            [
                "switch.b_heater",
                "light.b_spot",
                "switch.b_relay_left",
                "light.c_lamp",
                "climate.c_ac",
                "light.e_desk",
                "switch.e_plain",
                "light.e_strip",
                "fan.e_fan",
            ],
        )

    async def test_policies_list_every_participating_room_and_only_approved_devices(
        self,
    ):
        self.policies = POLICIES
        ranked, grouped, states = await self.rank()
        self.assertEqual(
            ranked,
            json.dumps(
                [
                    room("room-b", 2, ["light.b_spot", "switch.b_relay_left"]),
                    room("room-a", 1, ["light.a_ceiling", "fan.a_fan"]),
                    room("room-c", 0, []),
                ]
            ),
        )
        self.assertEqual(states, SERVED_CONTROLS)
        self.assertEqual(
            list(grouped), ["room-e", "room-c", "room-a", "room-d", "room-b"]
        )

    async def test_a_scope_without_participating_rooms_falls_back_to_activity(self):
        self.policies = POLICIES
        ranked, _, _ = await self.rank(["room-d", "room-e"])
        self.assertEqual(
            ranked, json.dumps([room("room-e", 2, ["light.e_desk", "fan.e_fan"])])
        )

    async def test_no_room_scope_and_nothing_on_rank_nothing(self):
        ranked, grouped, states = await self.rank([])
        self.assertEqual((ranked, grouped, states), ("[]", {}, []))
        for state in self.states.values.values():
            state.state = "off"
        ranked, _, _ = await self.rank()
        self.assertEqual(ranked, "[]")


if __name__ == "__main__":
    unittest.main()
