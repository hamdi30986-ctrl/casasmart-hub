"""Wall relays typed as a light or fan, end to end, against a real Home
Assistant.

Many homes here wire their lights through wall relays that Home Assistant
shows as switch entities (Sonoff, Tuya, Zigbee relays). The installer types
each gang in the app (the registry's gang type); a switch typed "light" or
"fan" is a light or fan to the hub: it joins the room's last-on set, room
off switches it off in a room without a stored policy, one tap brings it
back, and an admin may approve it or put it in a start profile. Plugs,
outlets, heaters and untyped switches never come on, and Home Assistant's
own "outlet" device class always wins. Room off also switches an
untyped relay off (most lights are on one), but one tap never turns it back
on. Same fixture as test_room_one_tap_ha,
with no stored policy and a Corridor wired only with relays. Real-HA
environment only.
"""

from __future__ import annotations

import os
import sys
import unittest
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H
from test_room_one_tap_ha import IMPORT_ERROR, OneTapCase

try:
    from casasmart.now_api import (
        CasaSmartRoomActivityPolicyView,
        CasaSmartRoomStartProfileView,
    )
except Exception as err:
    IMPORT_ERROR = IMPORT_ERROR or err

# device id -> (entity id, attributes, gangs record)
CORRIDOR = {
    "dev-corridor-light": (
        "switch.corridor_light",
        {},
        {"gangs": {"switch.corridor_light": {"type": "light"}}},
    ),
    "dev-corridor-fan": (
        "switch.corridor_fan",
        {},
        {"gangs": {"switch.corridor_fan": {"type": "fan"}}},
    ),
    # An older record that types its gangs by channel name.
    "dev-corridor-left": (
        "switch.corridor_left",
        {},
        {"gang_types": {"left": "light"}},
    ),
    "dev-corridor-plug": (
        "switch.corridor_plug",
        {"device_class": "outlet"},
        {"gangs": {"switch.corridor_plug": {"type": "outlet"}}},
    ),
    "dev-corridor-heater": (
        "switch.corridor_heater",
        {"device_class": "switch"},
        {"gangs": {"switch.corridor_heater": {"type": "heater"}}},
    ),
    # Typed light, but Home Assistant knows it is an outlet.
    "dev-corridor-socket": (
        "switch.corridor_socket",
        {"device_class": "outlet"},
        {"gangs": {"switch.corridor_socket": {"type": "light"}}},
    ),
    "dev-corridor-untyped": ("switch.corridor_untyped", {"device_class": "switch"}, {}),
}
RELAYS = ("switch.corridor_light", "switch.corridor_fan", "switch.corridor_left")
OTHERS = (
    "switch.corridor_plug",
    "switch.corridor_heater",
    "switch.corridor_socket",
    "switch.corridor_untyped",
)


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class WallRelayCase(OneTapCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.storage.table("now_room_policies").clear()
        self.storage.table("registry_rooms")["corridor"] = {
            "name": "Corridor",
            "floor_id": None,
            "icon": None,
            "sort_order": 4,
        }
        for device_id, (eid, attributes, record) in CORRIDOR.items():
            self.storage.table("registry_devices")[eid] = {
                "room_id": "corridor",
                "display_name": None,
                "sort_order": 0,
            }
            self.storage.table("registry_user_devices")[device_id] = {
                "entity_ids": [eid],
                **record,
            }
            self.hass.states.async_set(eid, "off", attributes)
        self.registry.warm_up()
        # As after a registry change: the recorder follows the new relays.
        await self.recorder._async_load_managed()
        await self.hass.async_block_till_done()
        _, self.admin = H.session(
            self.runtime.auth, role="admin", member_id="mem-admin"
        )

    async def put(self, view, room_id, body):
        return H.read_response(
            await view(self.hass).put(
                H.FakeRequest(headers=self.admin, body=body), room_id
            )
        )


class WallRelayOnlyRoom(WallRelayCase):
    async def test_a_room_with_only_wall_relays_gets_one_tap(self):
        for eid in (*RELAYS, *OTHERS):
            self.press(eid, "on")
        for eid in (*OTHERS, "switch.corridor_left", "switch.corridor_fan"):
            self.press(eid, "off")
        self.press("switch.corridor_light", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.last_on("corridor"),
            ["switch.corridor_light", "switch.corridor_fan", "switch.corridor_left"],
        )
        self.assertEqual(
            (await self.now_rooms())["corridor"],
            (
                3,
                {
                    "action": "turn_on",
                    "source": "last_on",
                    "light_count": 2,
                    "fan_count": 1,
                    "unavailable_count": 0,
                },
            ),
        )
        status, body = await self.turn_on("corridor")
        self.assertEqual((status, body["source"], body["ok"]), (200, "last_on", True))
        self.assertEqual(
            self.calls,
            [
                (
                    "switch",
                    "turn_on",
                    (
                        "switch.corridor_light",
                        "switch.corridor_fan",
                        "switch.corridor_left",
                    ),
                )
            ],
        )
        self.assertEqual(set(self.states(*OTHERS).values()), {"off"})

    async def test_room_off_switches_the_wall_relays_and_on_only_the_typed_ones(
        self,
    ):
        for eid in (*RELAYS, *OTHERS):
            self.press(eid, "on")
        status, off = await self.command("corridor", "turn_off")
        # The typed relays and the untyped one.
        self.assertEqual(
            (status, sorted(o["entity_id"] for o in off["outcomes"])),
            (200, sorted([*RELAYS, "switch.corridor_untyped"])),
        )
        # The plug, the heater and the socket keep running.
        self.assertEqual(
            self.states(*OTHERS),
            {
                "switch.corridor_plug": "on",
                "switch.corridor_heater": "on",
                "switch.corridor_socket": "on",
                "switch.corridor_untyped": "off",
            },
        )
        status, on = await self.turn_on("corridor")
        self.assertEqual((status, on["source"], on["ok"]), (200, "capture", True))
        self.assertEqual(set(self.states(*RELAYS).values()), {"on"})
        self.assertEqual(self.calls, [("switch", "turn_on", RELAYS)])
        # The untyped relay stays off: one tap never turns one on.
        self.assertEqual(
            self.states("switch.corridor_untyped"), {"switch.corridor_untyped": "off"}
        )

    async def test_a_relay_retyped_as_an_outlet_stops_coming_on(self):
        for eid in RELAYS:
            self.press(eid, "on")
        for eid in RELAYS:
            self.press(eid, "off")
        record = self.storage.table("registry_user_devices")["dev-corridor-light"]
        record["gangs"]["switch.corridor_light"]["type"] = "outlet"
        self.storage.table("registry_user_devices")["dev-corridor-light"] = record
        self.registry.warm_up()
        await self.recorder._async_load_managed()
        status, body = await self.turn_on("corridor")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.calls,
            [("switch", "turn_on", ("switch.corridor_left", "switch.corridor_fan"))],
        )
        self.assertEqual(
            self.states("switch.corridor_light"), {"switch.corridor_light": "off"}
        )


class InstallerSettings(WallRelayCase):
    async def test_an_admin_may_approve_a_typed_relay(self):
        status, body = await self.put(
            CasaSmartRoomActivityPolicyView,
            "corridor",
            {"participates": True, "eligible_entity_ids": ["switch.corridor_light"]},
        )
        self.assertEqual(
            (status, body["eligible_entity_ids"]), (200, ["switch.corridor_light"])
        )
        for eid in ("switch.corridor_plug", "switch.corridor_socket"):
            status, _ = await self.put(
                CasaSmartRoomActivityPolicyView,
                "corridor",
                {"participates": True, "eligible_entity_ids": [eid]},
            )
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, eid)

    async def test_a_start_profile_takes_a_typed_relay_only(self):
        status, body = await self.put(
            CasaSmartRoomStartProfileView,
            "corridor",
            {"entity_ids": ["switch.corridor_light", "switch.corridor_fan"]},
        )
        self.assertEqual(
            (status, body["entity_ids"]),
            (200, ["switch.corridor_light", "switch.corridor_fan"]),
        )
        for eid in OTHERS:
            status, body = await self.put(
                CasaSmartRoomStartProfileView, "corridor", {"entity_ids": [eid]}
            )
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, eid)
            self.assertIn("is not a light or fan", body["message"])
        self.assertEqual(
            (await self.now_rooms())["corridor"],
            (
                2,
                {
                    "action": "turn_on",
                    "source": "start_profile",
                    "light_count": 1,
                    "fan_count": 1,
                    "unavailable_count": 0,
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
