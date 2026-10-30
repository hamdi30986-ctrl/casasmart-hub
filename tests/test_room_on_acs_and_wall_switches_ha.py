"""Running ACs and untyped wall switches count as on, end to end, against
a real Home Assistant.

Most lights are on a wall switch, and a running AC is a room in use. So a room is
on by what room off switches (lights, fans, wall relays typed as one and now
untyped ones too) and by a running AC, which room off switches as well
(test_room_off_ac_ha). Plugs, outlets, heaters, covers and
TVs never count and are never switched. One tap "on" is unchanged but for an
AC in the hub's capture: lights, fans and relays typed as one, never an
untyped relay. Same fixture
as test_room_on_counts_room_off_ha (real core, recorder, views and stores,
no stored policy; a Server room with only an AC and a plug). Real-HA
environment only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H
from test_room_default_policy_ha import NOTHING_SAVED
from test_room_on_counts_room_off_ha import IMPORT_ERROR, RoomOnCase

try:
    from casasmart.now_api import CasaSmartRoomActivityPolicyView
except Exception as err:
    IMPORT_ERROR = IMPORT_ERROR or err

SWITCH = {"device_class": "switch"}
AC = {"hvac_modes": ["off", "cool", "dry"]}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class AcsAndWallSwitchesCase(RoomOnCase):
    def add_room(self, room_id, name, sort_order=5):
        self.storage.table("registry_rooms")[room_id] = {
            "name": name,
            "floor_id": None,
            "icon": None,
            "sort_order": sort_order,
        }
        self.registry.warm_up()

    def climate_calls(self):
        return [call for call in self.calls if call[0] == "climate"]


class RunningAcsCount(AcsAndWallSwitchesCase):
    async def test_an_ac_only_room_counts_one_and_shows_active(self):
        self.add_room("lounge", "Lounge")
        self.add("lounge", "dev-lounge-ac", [("climate.lounge_ac", "cool", AC)])
        rooms, mode = await self.rooms()
        lounge = rooms["lounge"]
        # active_count and eligible_entity_ids stay what room off switches.
        self.assertEqual(self.on(lounge), (0, True, 1, 1))
        self.assertEqual(
            (lounge["eligible_entity_ids"], lounge["rank"]["reason"]),
            ([], "active"),
        )
        # Not hidden behind "Everything is off".
        self.assertEqual(mode, "on")
        # Nothing for one tap to turn on: an AC never comes on at a guess.
        self.assertEqual(lounge["rank"]["one_tap"], NOTHING_SAVED)
        # Any running mode counts; off doesn't.
        self.press("climate.lounge_ac", "dry")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["lounge"]), (0, True, 1, 1))
        self.press("climate.lounge_ac", "off")
        self.press("climate.server_ac", "off")
        rooms, mode = await self.rooms()
        self.assertEqual(self.on(rooms["lounge"]), (0, False, 0, 0))
        self.assertEqual(mode, "all_off")

    async def test_an_ac_and_a_plug_count_one(self):
        rooms, _ = await self.rooms()
        server = rooms["server"]
        self.assertEqual(self.on(server), (0, True, 1, 1))
        self.assertEqual(server["eligible_entity_ids"], [])
        self.assertEqual(
            self.states("switch.server_plug"), {"switch.server_plug": "on"}
        )

    async def test_room_off_switches_the_ac_and_leaves_its_room_off(self):
        self.press("climate.majlis_ac", "cool")
        self.press("light.majlis_main", "on")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["majlis"]), (1, True, 2, 1))
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual(
            (status, off["ok"], [o["entity_id"] for o in off["outcomes"]]),
            (200, True, ["light.majlis_main", "climate.majlis_ac"]),
        )
        self.assertEqual(self.states("climate.majlis_ac"), {"climate.majlis_ac": "off"})
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["majlis"]), (0, False, 0, 0))
        # An AC on an admin's list (stored by hand: the API refuses one) is
        # switched once, as the room's AC.
        self.press("climate.majlis_ac", "cool")
        self.now_data.set_room_policy(
            "majlis", True, ["light.majlis_main", "climate.majlis_ac"]
        )
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual(
            (status, [o["entity_id"] for o in off["outcomes"]]),
            (200, ["climate.majlis_ac"]),
        )


class WallSwitchesCount(AcsAndWallSwitchesCase):
    async def test_an_untyped_wall_switch_counts_one_and_room_off_switches_it(self):
        self.add(
            "bedroom",
            "dev-bedroom-wall",
            [("switch.bedroom_wall", "on", SWITCH)],
            {"switch.bedroom_wall": {"type": "switch"}},
        )
        rooms, _ = await self.rooms()
        bedroom = rooms["bedroom"]
        self.assertEqual(self.on(bedroom), (1, True, 1, 0))
        self.assertIn("switch.bedroom_wall", bedroom["eligible_entity_ids"])
        status, off = await self.command("bedroom", "turn_off")
        self.assertEqual(
            (status, off["ok"], off["outcomes"]),
            (200, True, [{"entity_id": "switch.bedroom_wall", "outcome": "changed"}]),
        )
        self.assertEqual(
            self.states("switch.bedroom_wall"), {"switch.bedroom_wall": "off"}
        )
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["bedroom"]), (0, False, 0, 0))

    async def test_an_outlet_class_switch_counts_zero_and_is_not_switched(self):
        # Home Assistant calls them outlets: one untyped, one typed as a
        # light by mistake; and a relay the installer typed as an outlet.
        self.add(
            "bedroom",
            "dev-bedroom-sockets",
            [
                ("switch.bedroom_socket", "on", {"device_class": "outlet"}),
                ("switch.bedroom_lamp_socket", "on", {"device_class": "outlet"}),
                ("switch.bedroom_typed_plug", "on", SWITCH),
            ],
            {
                "switch.bedroom_lamp_socket": {"type": "light"},
                "switch.bedroom_typed_plug": {"type": "outlet"},
            },
        )
        sockets = (
            "switch.bedroom_socket",
            "switch.bedroom_lamp_socket",
            "switch.bedroom_typed_plug",
        )
        rooms, _ = await self.rooms()
        bedroom = rooms["bedroom"]
        self.assertEqual(self.on(bedroom), (0, False, 0, 0))
        self.assertEqual(bedroom["eligible_entity_ids"], ["light.bedroom"])
        status, off = await self.command("bedroom", "turn_off")
        self.assertEqual((status, off["outcomes"]), (200, []))
        self.assertEqual(set(self.states(*sockets).values()), {"on"})

    async def test_a_heater_is_never_switched(self):
        self.press("switch.kitchen_heater", "on")
        rooms, _ = await self.rooms()
        self.assertEqual(self.on(rooms["kitchen"]), (0, False, 0, 0))
        status, off = await self.command("kitchen", "turn_off")
        self.assertEqual((status, off["outcomes"]), (200, []))
        self.assertEqual(
            self.states("switch.kitchen_heater"), {"switch.kitchen_heater": "on"}
        )
        # An admin can't approve one any more; an untyped wall relay, yes.
        self.add(
            "kitchen", "dev-kitchen-wall", [("switch.kitchen_wall", "off", {})], {}
        )
        _, admin = H.session(self.runtime.auth, role="admin", member_id="mem-admin")
        for eid, expected in (
            ("switch.kitchen_heater", HTTPStatus.BAD_REQUEST),
            ("switch.kitchen_wall", HTTPStatus.OK),
        ):
            status, _ = H.read_response(
                await CasaSmartRoomActivityPolicyView(self.hass).put(
                    H.FakeRequest(
                        headers=admin,
                        body={"participates": True, "eligible_entity_ids": [eid]},
                    ),
                    "kitchen",
                )
            )
            self.assertEqual(status, expected, eid)

    async def test_one_tap_does_not_turn_on_an_untyped_switch(self):
        self.add(
            "bedroom",
            "dev-bedroom-wall",
            [("switch.bedroom_wall", "on", {})],
            {"switch.bedroom_wall": {"type": "switch"}},
        )
        self.press("light.bedroom", "on")
        status, off = await self.command("bedroom", "turn_off")
        self.assertEqual(
            (status, sorted(o["entity_id"] for o in off["outcomes"])),
            (200, ["light.bedroom", "switch.bedroom_wall"]),
        )
        # The card offers the light only.
        rooms, _ = await self.rooms()
        self.assertEqual(
            rooms["bedroom"]["rank"]["one_tap"],
            {
                "action": "turn_on",
                "source": "capture",
                "light_count": 1,
                "fan_count": 0,
                "unavailable_count": 0,
            },
        )
        status, on = await self.turn_on("bedroom")
        self.assertEqual((status, on["ok"]), (200, True))
        self.assertEqual(self.calls, [("light", "turn_on", ("light.bedroom",))])
        self.assertEqual(
            self.states("switch.bedroom_wall"), {"switch.bedroom_wall": "off"}
        )
        # A room with only an untyped relay: room off switches it, one tap
        # has nothing to turn on and opens the room.
        self.add_room("store", "Store")
        self.add("store", "dev-store-wall", [("switch.store_wall", "on", {})])
        status, off = await self.command("store", "turn_off")
        self.assertEqual(
            (status, [o["entity_id"] for o in off["outcomes"]]),
            (200, ["switch.store_wall"]),
        )
        rooms, _ = await self.rooms()
        self.assertEqual(rooms["store"]["rank"]["one_tap"], NOTHING_SAVED)
        status, body = await self.turn_on("store")
        self.assertEqual(
            (status, body["error"]), (HTTPStatus.CONFLICT, "nothing_to_turn_on")
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states("switch.store_wall"), {"switch.store_wall": "off"})

    async def test_an_untyped_wall_switch_off_is_the_room_going_dark(self):
        self.add(
            "bedroom",
            "dev-bedroom-wall",
            [("switch.bedroom_wall", "off", {})],
            {"switch.bedroom_wall": {"type": "switch"}},
        )
        await self.recorder._async_load_managed()
        await asyncio.sleep(0.1)  # past the recorder's (test) settle
        self.press("switch.bedroom_wall", "on")
        await self.hass.async_block_till_done()
        self.press("switch.bedroom_wall", "off")
        rooms, _ = await self.rooms()
        self.assertEqual(rooms["bedroom"]["rank"]["reason"], "just_off")


if __name__ == "__main__":
    unittest.main()
