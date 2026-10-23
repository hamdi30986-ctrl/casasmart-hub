"""Rooms without a stored policy, end to end, against a real Home Assistant.

A room an admin never set a policy for takes part in room commands with its
lights and fans, as rooms do in Apple Home and Google Home: room off switches
them off, one tap "on" brings them back, and the Now page offers the tap. An
admin's stored policy still wins (participates false, or a list of its own).
Never a plug, outlet, heater, AC, cover or TV. Same fixture as
test_room_one_tap_ha (real core, recorder, views and stores), with every
stored policy removed. Real-HA environment only.
"""

from __future__ import annotations

import os
import sys
import unittest
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H
from test_room_one_tap_ha import IMPORT_ERROR, OneTapCase, last_on

try:
    from casasmart.now_api import CasaSmartNowView
except Exception as err:
    IMPORT_ERROR = IMPORT_ERROR or err

DISABLED = {"action": "open_room", "reason": "room_activity_disabled"}
NOTHING_SAVED = {"action": "open_room", "reason": "nothing_saved"}
# What the Majlis has besides its lights and fan, all on.
MAJLIS_OTHERS = {
    "switch.majlis_outlet": "on",
    "climate.majlis_ac": "cool",
    "cover.majlis_curtain": "open",
    "media_player.majlis_tv": "playing",
    "light.majlis_lamp": "on",
}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class DefaultPolicyCase(OneTapCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.storage.table("now_room_policies").clear()
        # A room with nothing one tap may turn on: an AC and a plug.
        storage = self.storage
        storage.table("registry_rooms")["server"] = {
            "name": "Server",
            "floor_id": None,
            "icon": None,
            "sort_order": 3,
        }
        for eid, state, attributes, gangs in (
            ("climate.server_ac", "cool", {"hvac_modes": ["off", "cool"]}, {}),
            (
                "switch.server_plug",
                "on",
                {"device_class": "outlet"},
                {"switch.server_plug": {"type": "outlet"}},
            ),
        ):
            storage.table("registry_devices")[eid] = {
                "room_id": "server",
                "display_name": None,
                "sort_order": 0,
            }
            storage.table("registry_user_devices")[f"dev-{eid}"] = {
                "entity_ids": [eid],
                "gangs": gangs,
            }
            self.hass.states.async_set(eid, state, attributes)
        self.registry.warm_up()
        for domain, services in (("climate", ("turn_on", "turn_off")),):
            for service in services:
                self.hass.services.async_register(domain, service, self._record)
        await self.hass.async_block_till_done()

    async def now_body(self, headers=None):
        await self.hass.async_block_till_done()
        status, body = H.read_response(
            await CasaSmartNowView(self.hass).get(
                H.FakeRequest(headers=headers or self.headers)
            )
        )
        self.assertEqual(status, 200)
        return body["room_activity"]


class RoomsTakePartByDefault(DefaultPolicyCase):
    async def test_room_off_and_on_switch_only_the_lights_and_fans(self):
        for eid in ("light.majlis_main", "light.majlis_spot", "fan.majlis"):
            self.press(eid, "on")
        for eid, state in MAJLIS_OTHERS.items():
            self.press(eid, state)
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual((status, off["ok"]), (200, True))
        self.assertEqual(
            sorted(o["entity_id"] for o in off["outcomes"]),
            ["fan.majlis", "light.majlis_main", "light.majlis_spot"],
        )
        # The plug, the AC, the curtain, the TV and the lamp left out of
        # room activity keep running.
        self.assertEqual(self.states(*MAJLIS_OTHERS), MAJLIS_OTHERS)
        self.assertEqual(
            (await self.now_rooms())["majlis"],
            (3, {**last_on(2, 1), "source": "capture"}),
        )

        status, on = await self.turn_on("majlis")
        self.assertEqual((status, on["source"], on["ok"]), (200, "capture", True))
        self.assertEqual(
            self.calls,
            [
                ("light", "turn_on", ("light.majlis_main", "light.majlis_spot")),
                ("fan", "turn_on", ("fan.majlis",)),
            ],
        )
        self.assertEqual(self.states(*MAJLIS_OTHERS), MAJLIS_OTHERS)

    async def test_a_room_switched_off_at_the_wall_turns_back_on(self):
        # The heater relay and the light not imported into the hub are in the
        # Kitchen too: never switched by a room command.
        for eid in ("light.kitchen", "fan.kitchen_hood", "switch.kitchen_heater"):
            self.press(eid, "on")
        self.press("light.kitchen_unimported", "on")
        for eid in ("fan.kitchen_hood", "light.kitchen", "switch.kitchen_heater"):
            self.press(eid, "off")
        self.assertEqual((await self.now_rooms())["kitchen"], (2, last_on(1, 1)))
        status, body = await self.turn_on("kitchen")
        self.assertEqual((status, body["source"], body["ok"]), (200, "last_on", True))
        self.assertEqual(self.switched(), [("light", "turn_on"), ("fan", "turn_on")])
        self.assertEqual(
            self.states("switch.kitchen_heater", "light.kitchen_unimported"),
            {"switch.kitchen_heater": "off", "light.kitchen_unimported": "on"},
        )
        status, off = await self.command("kitchen", "turn_off")
        self.assertEqual(
            sorted(o["entity_id"] for o in off["outcomes"]),
            ["fan.kitchen_hood", "light.kitchen"],
        )
        self.assertEqual(
            self.states("light.kitchen_unimported"), {"light.kitchen_unimported": "on"}
        )

    async def test_a_start_profile_works_without_a_policy(self):
        status, _ = await self.start_profile("bedroom", ["light.bedroom"])
        self.assertEqual(status, 200)
        self.assertEqual(
            (await self.now_rooms())["bedroom"],
            (1, {**last_on(1), "source": "start_profile"}),
        )
        status, body = await self.turn_on("bedroom")
        self.assertEqual((status, body["source"]), (200, "start_profile"))
        self.assertEqual(self.states("light.bedroom"), {"light.bedroom": "on"})

    async def start_profile(self, room_id, entity_ids):
        from casasmart.now_api import CasaSmartRoomStartProfileView

        _, admin = H.session(self.runtime.auth, role="admin", member_id="mem-admin")
        return H.read_response(
            await CasaSmartRoomStartProfileView(self.hass).put(
                H.FakeRequest(headers=admin, body={"entity_ids": entity_ids}),
                room_id,
            )
        )

    async def test_nothing_known_or_nothing_to_turn_on_opens_the_room(self):
        rooms = await self.now_rooms()
        self.assertEqual(rooms["bedroom"], (0, NOTHING_SAVED))
        # Only an AC and a plug: no ON button, ever.
        self.assertEqual(rooms["server"], (0, NOTHING_SAVED))
        for room_id in ("bedroom", "server"):
            status, body = await self.turn_on(room_id)
            self.assertEqual(
                (status, body["error"], body["open_room"]),
                (HTTPStatus.CONFLICT, "nothing_to_turn_on", True),
            )
        self.assertEqual(self.calls, [])
        status, off = await self.command("server", "turn_off")
        self.assertEqual((status, off["outcomes"]), (200, []))
        self.assertEqual(
            self.states("climate.server_ac", "switch.server_plug"),
            {"climate.server_ac": "cool", "switch.server_plug": "on"},
        )

    async def test_configured_room_count_counts_only_stored_policies(self):
        self.assertEqual((await self.now_body())["configured_room_count"], 0)


class AStoredPolicyWins(DefaultPolicyCase):
    async def test_an_admin_takes_a_room_out(self):
        self.press("light.majlis_main", "on")
        self.press("light.majlis_main", "off")
        self.now_data.set_room_policy("majlis", False, [])
        self.assertEqual((await self.now_rooms())["majlis"], (0, DISABLED))
        for action in ("turn_on", "turn_off"):
            status, body = await self.command("majlis", action)
            self.assertEqual(
                (status, body["error"]),
                (HTTPStatus.CONFLICT, "room_activity_disabled"),
            )
        self.assertEqual(self.calls, [])
        self.assertEqual((await self.now_body())["configured_room_count"], 0)

    async def test_an_admins_own_list(self):
        self.now_data.set_room_policy("majlis", True, ["light.majlis_main"])
        for eid in ("light.majlis_main", "light.majlis_spot", "fan.majlis"):
            self.press(eid, "on")
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual(
            (status, off["outcomes"]),
            (200, [{"entity_id": "light.majlis_main", "outcome": "changed"}]),
        )
        self.assertEqual(
            self.states("light.majlis_spot", "fan.majlis"),
            {"light.majlis_spot": "on", "fan.majlis": "on"},
        )
        body = await self.now_body()
        self.assertEqual(body["configured_room_count"], 1)
        # A stored policy that takes part lists every room that takes part:
        # the rooms without one too, with their lights and fans.
        self.assertEqual(
            {r["room_id"]: r["eligible_entity_ids"] for r in body["rooms"]},
            {
                "majlis": ["light.majlis_main"],
                "kitchen": ["light.kitchen", "fan.kitchen_hood"],
                "bedroom": ["light.bedroom"],
                "server": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
