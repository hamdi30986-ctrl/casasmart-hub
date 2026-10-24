"""A room is on only by what room off switches, end to end, against a
real Home Assistant.

A room card counted plugs and untyped switches as "on"
although room off never switches them, so a fridge plug kept a kitchen "on"
forever and room off seemed not to work. Now a room
is on only by what room off switches; a plug, an outlet, a heater, a cover or
a TV never makes it on. Room off later widened to
untyped wall relays, and a running AC makes its room on although
room off leaves it alone (test_room_on_acs_and_wall_switches_ha). Same
fixture as test_room_default_policy_ha (real core, recorder, views and
stores, no stored policy; a Server room with only an AC and a plug).
Real-HA environment only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_room_default_policy_ha import IMPORT_ERROR, DefaultPolicyCase

POWER_W = {"device_class": "power", "unit_of_measurement": "W"}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class RoomOnCase(DefaultPolicyCase):
    def add(self, room_id, device_id, entities, gangs=None):
        """Import a device into a room: entities are (entity id, state,
        attributes)."""
        for eid, state, attributes in entities:
            self.storage.table("registry_devices")[eid] = {
                "room_id": room_id,
                "display_name": None,
                "sort_order": 0,
            }
            self.hass.states.async_set(eid, state, attributes)
        self.storage.table("registry_user_devices")[device_id] = {
            "entity_ids": [eid for eid, _, _ in entities],
            "gangs": gangs or {},
        }
        self.registry.warm_up()

    async def rooms(self):
        """(rooms by id, ranking mode) from the Now page."""
        activity = await self.now_body()
        return (
            {room["room_id"]: room for room in activity["rooms"]},
            activity["ranking"]["mode"],
        )

    def on(self, room):
        """(active_count, rank.active, rank.on_count, rank.ac_on_count)."""
        rank = room["rank"]
        return (
            room["active_count"],
            rank["active"],
            rank["on_count"],
            rank["ac_on_count"],
        )


class RoomOnIsWhatRoomOffSwitches(RoomOnCase):
    async def test_a_fridge_plug_alone_leaves_its_room_off(self):
        self.add(
            "kitchen",
            "dev-kitchen-fridge",
            [
                ("switch.kitchen_fridge", "on", {"device_class": "outlet"}),
                ("sensor.kitchen_fridge_power", "120", POWER_W),
            ],
            {"switch.kitchen_fridge": {"type": "outlet"}},
        )
        rooms, mode = await self.rooms()
        kitchen = rooms["kitchen"]
        self.assertEqual(self.on(kitchen), (0, False, 0, 0))
        # Not still running either: the plug's meter isn't the room's.
        self.assertEqual(
            (kitchen["rank"]["reason"], kitchen["rank"]["watts"]), ("room_order", None)
        )
        self.assertNotIn("switch.kitchen_fridge", kitchen["eligible_entity_ids"])
        # The Server room has only a running AC and a plug on: on by its AC
        # alone.
        self.assertEqual(self.on(rooms["server"]), (0, True, 1, 1))
        self.assertEqual(mode, "on")

    async def test_a_plug_and_a_light_on_count_one_and_room_off_leaves_it_off(self):
        self.press("light.majlis_main", "on")
        self.press("switch.majlis_outlet", "on")
        rooms, mode = await self.rooms()
        majlis = rooms["majlis"]
        self.assertEqual(self.on(majlis), (1, True, 1, 0))
        self.assertEqual(mode, "on")
        self.assertNotIn("switch.majlis_outlet", majlis["eligible_entity_ids"])
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual((status, off["ok"]), (200, True))
        self.assertEqual(
            [o["entity_id"] for o in off["outcomes"]], ["light.majlis_main"]
        )
        self.assertEqual(
            self.states("switch.majlis_outlet"), {"switch.majlis_outlet": "on"}
        )
        # The card agrees with room off: the plug left on doesn't keep the
        # Majlis on (the Server's running AC keeps the page "on").
        rooms, mode = await self.rooms()
        self.assertEqual(self.on(rooms["majlis"]), (0, False, 0, 0))
        self.assertEqual(mode, "on")

    async def test_a_relay_typed_as_a_light_counts_and_room_off_switches_it(self):
        self.press("switch.majlis_relay", "on")
        rooms, _ = await self.rooms()
        majlis = rooms["majlis"]
        self.assertEqual(self.on(majlis), (1, True, 1, 0))
        self.assertIn("switch.majlis_relay", majlis["eligible_entity_ids"])
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual(
            (status, [o["entity_id"] for o in off["outcomes"]]),
            (200, ["switch.majlis_relay"]),
        )
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["majlis"]), (0, False, 0, 0))

    async def test_an_unmarked_switch_counts_and_room_off_switches_it(self):
        # These were once left on and not counted; an untyped wall relay is
        # most often a light's.
        self.add(
            "bedroom",
            "dev-bedroom-relay",
            [("switch.bedroom_relay", "on", {"device_class": "switch"})],
            {"switch.bedroom_relay": {"type": "switch"}},
        )
        self.add(
            "bedroom", "dev-bedroom-untyped", [("switch.bedroom_untyped", "on", {})]
        )
        rooms, _ = await self.rooms()
        bedroom = rooms["bedroom"]
        self.assertEqual(self.on(bedroom), (2, True, 2, 0))
        self.assertEqual(
            bedroom["eligible_entity_ids"],
            ["light.bedroom", "switch.bedroom_relay", "switch.bedroom_untyped"],
        )
        status, off = await self.command("bedroom", "turn_off")
        self.assertEqual(
            (status, off["ok"], sorted(o["entity_id"] for o in off["outcomes"])),
            (200, True, ["switch.bedroom_relay", "switch.bedroom_untyped"]),
        )
        self.assertEqual(
            self.states("switch.bedroom_relay", "switch.bedroom_untyped"),
            {"switch.bedroom_relay": "off", "switch.bedroom_untyped": "off"},
        )
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["bedroom"]), (0, False, 0, 0))

    async def test_a_heater_on_a_stored_list_is_never_switched_nor_counted(self):
        # A list stored earlier, when a heater relay with device class
        # switch could be approved; room commands now skip it.
        self.now_data.set_room_policy(
            "kitchen", True, ["light.kitchen", "switch.kitchen_heater"]
        )
        self.press("switch.kitchen_heater", "on")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["kitchen"]), (0, False, 0, 0))
        self.press("light.kitchen", "on")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["kitchen"]), (1, True, 1, 0))
        status, off = await self.command("kitchen", "turn_off")
        self.assertEqual(
            (status, [o["entity_id"] for o in off["outcomes"]]),
            (200, ["light.kitchen"]),
        )
        self.assertEqual(
            self.states("switch.kitchen_heater"), {"switch.kitchen_heater": "on"}
        )
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["kitchen"]), (0, False, 0, 0))

    async def test_a_plug_switched_off_is_not_the_room_going_dark(self):
        await asyncio.sleep(0.1)  # past the recorder's (test) settle
        self.press("switch.majlis_outlet", "on")
        await self.hass.async_block_till_done()
        self.press("switch.majlis_outlet", "off")
        rooms, _ = await self.rooms()
        self.assertEqual(rooms["majlis"]["rank"]["reason"], "room_order")
        self.press("light.majlis_main", "on")
        await self.hass.async_block_till_done()
        self.press("light.majlis_main", "off")
        rooms, _ = await self.rooms()
        self.assertEqual(rooms["majlis"]["rank"]["reason"], "just_off")


if __name__ == "__main__":
    unittest.main()
