"""A room is on only by what room off switches, end to end, against a
real Home Assistant.

A room card counted plugs and untyped switches as "on"
although room off never switches them, so a fridge plug kept a kitchen "on"
forever and room off seemed not to work. Now a room
is on only by its lights and fans that room off switches (wall relays typed
as one included); a plug, an outlet, an untyped switch, a heater, an AC, a
cover or a TV never makes it on. Same fixture as test_room_default_policy_ha
(real core, recorder, views and stores, no stored policy; a Server room with
only an AC and a plug). Real-HA environment only.
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
class RoomOnIsWhatRoomOffSwitches(DefaultPolicyCase):
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
        # The Server room has only a running AC and a plug on: off too.
        self.assertEqual(self.on(rooms["server"]), (0, False, 0, 0))
        self.assertEqual(mode, "all_off")

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
        # Majlis on.
        rooms, mode = await self.rooms()
        self.assertEqual(self.on(rooms["majlis"]), (0, False, 0, 0))
        self.assertEqual(mode, "all_off")

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

    async def test_an_unmarked_switch_does_not_count(self):
        self.add(
            "bedroom",
            "dev-bedroom-relay",
            [("switch.bedroom_relay", "on", {"device_class": "switch"})],
            {"switch.bedroom_relay": {"type": "switch"}},
        )
        self.add(
            "bedroom", "dev-bedroom-untyped", [("switch.bedroom_untyped", "on", {})]
        )
        rooms, mode = await self.rooms()
        bedroom = rooms["bedroom"]
        self.assertEqual(self.on(bedroom), (0, False, 0, 0))
        self.assertEqual(bedroom["eligible_entity_ids"], ["light.bedroom"])
        self.assertEqual(mode, "all_off")
        # Room off has nothing of the room's to switch, and leaves them on.
        status, off = await self.command("bedroom", "turn_off")
        self.assertEqual((status, off["ok"], off["outcomes"]), (200, True, []))
        self.assertEqual(
            self.states("switch.bedroom_relay", "switch.bedroom_untyped"),
            {"switch.bedroom_relay": "on", "switch.bedroom_untyped": "on"},
        )

    async def test_a_stored_policy_may_switch_a_heater_off_but_it_never_counts(self):
        self.now_data.set_room_policy(
            "kitchen", True, ["light.kitchen", "switch.kitchen_heater"]
        )
        self.press("switch.kitchen_heater", "on")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["kitchen"]), (0, False, 0, 0))
        self.press("light.kitchen", "on")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["kitchen"]), (1, True, 1, 0))
        # The admin's list: room off switches both off, and the room is off.
        status, off = await self.command("kitchen", "turn_off")
        self.assertEqual(
            (status, sorted(o["entity_id"] for o in off["outcomes"])),
            (200, ["light.kitchen", "switch.kitchen_heater"]),
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
