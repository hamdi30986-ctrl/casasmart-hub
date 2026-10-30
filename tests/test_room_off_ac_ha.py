"""Room off switches the room's running ACs too, and one tap brings them back
from a fresh capture only, end to end, against a real Home Assistant.

An AC-only room turns off with the same switch. Each AC comes back at the mode
and target it had, by the saved-scene path (one step at a time, no fan step),
and never without a capture of the hub's own room off. A device with
casasmart_room_activity_exclude stays out, as do plugs, outlets, heaters,
covers and TVs. Same fixture as test_room_on_counts_room_off_ha (real core,
recorder, views and stores; a Server room with only an AC and a plug), with
ACs that follow their commands. Real-HA environment only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H
from test_room_on_counts_room_off_ha import IMPORT_ERROR, RoomOnCase

try:
    from casasmart.now_api import CasaSmartRoomActivityCommandView
    from homeassistant.exceptions import HomeAssistantError
except Exception as err:
    IMPORT_ERROR = IMPORT_ERROR or err

AC = {
    "hvac_modes": ["off", "cool", "heat", "dry", "fan_only"],
    "temperature": 22,
    "min_temp": 16,
    "max_temp": 30,
    "supported_features": 9,
    "fan_mode": "high",
    "fan_modes": ["low", "high"],
}
OFF = ("set_hvac_mode", {"hvac_mode": "off"})
COOL = ("set_hvac_mode", {"hvac_mode": "cool"})
AT_22 = ("set_temperature", {"temperature": 22})


def one_tap(lights=0, fans=0, *, acs=0, unavailable=0, source="capture"):
    tap = {
        "action": "turn_on",
        "source": source,
        "light_count": lights,
        "fan_count": fans,
        "unavailable_count": unavailable,
    }
    return {**tap, "ac_count": acs} if acs else tap


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class RoomOffAcCase(RoomOnCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ac_commands = []
        self.hass.states.async_set("climate.majlis_ac", "off", AC)
        self.hass.states.async_set("climate.server_ac", "cool", AC)
        await self.hass.async_block_till_done()

    async def _climate(self, call):
        self.ac_commands.append(
            (
                call.service,
                {key: value for key, value in call.data.items() if key != "entity_id"},
            )
        )
        await super()._climate(call)

    def outcomes(self, body):
        return {item["entity_id"]: item["outcome"] for item in body["outcomes"]}

    async def off(self, room_id, **kwargs):
        self.ac_commands.clear()
        self.calls.clear()
        return await self.command(room_id, "turn_off", **kwargs)

    async def on(self, room_id, **kwargs):
        self.ac_commands.clear()
        return await self.turn_on(room_id, **kwargs)

    def counts(self, room):
        return super().on(room)

    def age_capture(self, room_id, **delta):
        record = self.storage.table("now_restore_sets")[room_id]
        self.storage.table("now_restore_sets")[room_id] = {
            **record,
            "captured_at": (datetime.now(UTC) - timedelta(**delta)).isoformat(),
        }

    async def majlis_running(self):
        self.press("light.majlis_main", "on")
        self.press("climate.majlis_ac", "cool")
        await self.hass.async_block_till_done()

    def broken(self, service):
        async def handler(call):
            raise HomeAssistantError("the AC didn't answer")

        self.hass.services.async_register("climate", service, handler)

    def fixed(self, service):
        self.hass.services.async_register("climate", service, self._climate)


class RoomOffTakesTheAc(RoomOffAcCase):
    async def test_off_then_one_tap_brings_its_mode_and_target_back(self):
        await self.majlis_running()
        status, off = await self.off("majlis")
        self.assertEqual((status, off["ok"]), (200, True))
        self.assertEqual(
            self.outcomes(off),
            {"light.majlis_main": "changed", "climate.majlis_ac": "changed"},
        )
        self.assertEqual(self.ac_commands, [OFF])
        self.assertEqual(off["restore_pending_count"], 2)
        self.assertEqual(self.states("climate.majlis_ac"), {"climate.majlis_ac": "off"})
        self.assertEqual(
            self.now_data.restore_acs("majlis"),
            {"climate.majlis_ac": {"hvac_mode": "cool", "temperature": 22}},
        )
        rooms, _ = await self.rooms()
        self.assertEqual(self.counts(rooms["majlis"]), (0, False, 0, 0))
        self.assertEqual(rooms["majlis"]["restore_pending_count"], 2)
        self.assertEqual(rooms["majlis"]["rank"]["one_tap"], one_tap(1, acs=1))

        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"], on["source"]), (200, True, "capture"))
        self.assertEqual(
            self.outcomes(on),
            {"light.majlis_main": "changed", "climate.majlis_ac": "changed"},
        )
        # The mode, then the target; never the fan, which an IR AC would miss.
        self.assertEqual(self.ac_commands, [COOL, AT_22])
        self.assertEqual(
            self.states("climate.majlis_ac"), {"climate.majlis_ac": "cool"}
        )
        self.assertEqual(
            (
                on["restored_count"],
                on["restored_from_capture_count"],
                on["restore_pending_count"],
            ),
            (2, 2, 0),
        )
        self.assertEqual(self.now_data.restore_acs("majlis"), {})

    async def test_an_ac_only_room_turns_off_and_back_on_with_the_same_switch(self):
        rooms, _ = await self.rooms()
        self.assertEqual(self.counts(rooms["server"]), (0, True, 1, 1))
        status, off = await self.off("server")
        self.assertEqual(
            (status, off["ok"], self.outcomes(off)),
            (200, True, {"climate.server_ac": "changed"}),
        )
        self.assertEqual(
            self.states("climate.server_ac", "switch.server_plug"),
            {"climate.server_ac": "off", "switch.server_plug": "on"},
        )
        rooms, _ = await self.rooms()
        self.assertEqual(self.counts(rooms["server"]), (0, False, 0, 0))
        self.assertEqual(
            (
                rooms["server"]["restore_pending_count"],
                rooms["server"]["rank"]["one_tap"],
            ),
            (1, one_tap(acs=1)),
        )
        status, on = await self.on("server")
        self.assertEqual((status, on["ok"], on["source"]), (200, True, "capture"))
        self.assertEqual(self.ac_commands, [COOL, AT_22])
        self.assertEqual(
            self.states("climate.server_ac", "switch.server_plug"),
            {"climate.server_ac": "cool", "switch.server_plug": "on"},
        )

    async def test_the_activity_log_knows_it_was_the_members_off(self):
        await asyncio.sleep(0.1)  # past the recorder's (test) settle
        self.press("climate.majlis_ac", "cool")
        await self.off("majlis")
        await self.recorder.async_flush()
        events = await self.recorder.async_recent_events(0, ["majlis"])
        self.assertIn(
            ("climate.majlis_ac", "off", "app", "mem-a"),
            [
                (
                    event["entity_id"],
                    event["action"],
                    event["source"],
                    event["member_id"],
                )
                for event in events
            ],
        )

    async def test_an_off_that_found_only_the_ac_brings_back_only_the_ac(self):
        # The room has a last-on set from earlier, at the wall.
        self.press("light.majlis_main", "on")
        self.press("light.majlis_main", "off")
        self.press("climate.majlis_ac", "cool")
        await self.off("majlis")
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"]), (200, "capture"))
        self.assertEqual(self.ac_commands, [COOL, AT_22])
        self.assertEqual(self.states("light.majlis_main"), {"light.majlis_main": "off"})

    async def test_a_room_with_a_stored_policy_takes_its_ac_too(self):
        self.now_data.set_room_policy("majlis", True, ["light.majlis_main"])
        await self.majlis_running()
        status, off = await self.off("majlis")
        self.assertEqual(
            (status, self.outcomes(off)),
            (200, {"light.majlis_main": "changed", "climate.majlis_ac": "changed"}),
        )

    async def test_an_ac_with_no_target_comes_back_in_its_mode(self):
        attributes = {**AC, "temperature": None}
        self.hass.states.async_set("climate.majlis_ac", "dry", attributes)
        await self.off("majlis")
        self.assertEqual(
            self.now_data.restore_acs("majlis"),
            {"climate.majlis_ac": {"hvac_mode": "dry"}},
        )
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, True))
        self.assertEqual(self.ac_commands, [("set_hvac_mode", {"hvac_mode": "dry"})])
        self.assertEqual(self.states("climate.majlis_ac"), {"climate.majlis_ac": "dry"})

    async def test_a_repeated_off_keeps_the_capture(self):
        await self.majlis_running()
        await self.off("majlis")
        status, again = await self.off("majlis")
        self.assertEqual((status, again["outcomes"]), (200, []))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(again["restore_pending_count"], 2)
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, True))
        self.assertEqual(self.ac_commands, [COOL, AT_22])

    async def test_a_retry_with_the_same_key_replays_the_answer(self):
        await self.majlis_running()
        view = CasaSmartRoomActivityCommandView(self.hass)

        async def post():
            return H.read_response(
                await view.post(
                    H.FakeRequest(
                        headers=self.headers,
                        body={"action": "turn_off", "idempotency_key": "ac-off-1"},
                    ),
                    "majlis",
                )
            )

        first = await post()
        self.assertEqual(self.ac_commands, [OFF])
        self.press("climate.majlis_ac", "cool")
        self.assertEqual(await post(), first)
        self.assertEqual(self.ac_commands, [OFF])
        self.assertEqual(
            self.states("climate.majlis_ac"), {"climate.majlis_ac": "cool"}
        )


class WhatStaysOut(RoomOffAcCase):
    async def test_a_device_left_out_of_room_activity_stays_untouched(self):
        self.hass.states.async_set(
            "climate.server_ac",
            "cool",
            {**AC, "casasmart_room_activity_exclude": True},
        )
        rooms, _ = await self.rooms()
        # Nothing of the room's counts now: it is off, with nothing to switch.
        self.assertNotIn("server", rooms)
        status, off = await self.off("server")
        self.assertEqual((status, off["outcomes"]), (200, []))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(
            self.states("climate.server_ac"), {"climate.server_ac": "cool"}
        )
        self.assertEqual(self.now_data.restore_acs("server"), {})

    async def test_an_ac_left_out_after_the_off_stays_off(self):
        await self.off("server")
        self.hass.states.async_set(
            "climate.server_ac", "off", {**AC, "casasmart_room_activity_exclude": True}
        )
        status, body = await self.on("server")
        self.assertEqual(
            (status, body["error"]), (HTTPStatus.CONFLICT, "nothing_to_turn_on")
        )
        self.assertEqual(self.ac_commands, [])

    async def test_a_heat_only_thermostat_stays_untouched(self):
        self.add(
            "server",
            "dev-server-heat",
            [("climate.server_heat", "heat", {"hvac_modes": ["off", "heat"]})],
        )
        status, off = await self.off("server")
        self.assertEqual(
            (status, self.outcomes(off)), (200, {"climate.server_ac": "changed"})
        )
        self.assertEqual(
            self.states("climate.server_heat"), {"climate.server_heat": "heat"}
        )

    async def test_an_ac_the_hub_doesnt_manage_stays_untouched(self):
        self.storage.table("registry_devices")["climate.server_loose"] = {
            "room_id": "server",
            "display_name": None,
            "sort_order": 0,
        }
        self.registry.warm_up()
        self.hass.states.async_set("climate.server_loose", "cool", AC)
        status, off = await self.off("server")
        self.assertEqual(
            (status, self.outcomes(off)), (200, {"climate.server_ac": "changed"})
        )
        self.assertEqual(
            self.states("climate.server_loose"), {"climate.server_loose": "cool"}
        )

    async def test_plugs_heaters_covers_and_tvs_stay_as_they_are(self):
        others = {
            "switch.majlis_outlet": "on",
            "cover.majlis_curtain": "open",
            "media_player.majlis_tv": "playing",
            "light.majlis_lamp": "on",
        }
        for eid, state in others.items():
            self.press(eid, state)
        self.add(
            "majlis",
            "dev-majlis-heater",
            [("switch.majlis_heater", "on", {"device_class": "switch"})],
            {"switch.majlis_heater": {"type": "heater"}},
        )
        others["switch.majlis_heater"] = "on"
        await self.majlis_running()
        status, off = await self.off("majlis")
        self.assertEqual(
            (status, self.outcomes(off)),
            (200, {"light.majlis_main": "changed", "climate.majlis_ac": "changed"}),
        )
        self.assertEqual(self.states(*others), others)
        status, on = await self.on("majlis")
        self.assertEqual(status, 200)
        self.assertEqual(self.states(*others), others)

    async def test_another_rooms_ac_stays_on(self):
        await self.majlis_running()
        await self.off("majlis")
        self.assertEqual(
            self.states("climate.server_ac"), {"climate.server_ac": "cool"}
        )

    async def test_a_room_out_of_scope_is_refused_and_its_ac_stays_on(self):
        _, scoped = H.session(
            self.runtime.auth, role="user", rooms=["kitchen"], member_id="mem-b"
        )
        status, _ = await self.off("server", headers=scoped)
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(
            self.states("climate.server_ac"), {"climate.server_ac": "cool"}
        )

    async def test_a_room_taken_out_of_room_commands_keeps_its_ac(self):
        self.now_data.set_room_policy("server", False, [])
        status, body = await self.off("server")
        self.assertEqual(
            (status, body["error"]), (HTTPStatus.CONFLICT, "room_activity_disabled")
        )
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(
            self.states("climate.server_ac"), {"climate.server_ac": "cool"}
        )

    async def test_the_energy_saving_lockout_refuses_both_ways(self):
        await self.off("server")
        self.press("climate.majlis_ac", "cool")
        self.runtime.energy = types.SimpleNamespace(
            snapshot=lambda: {"active": True, "lockout_enabled": True}
        )
        for command in (self.off("majlis"), self.on("server")):
            status, body = await command
            self.assertEqual(
                (status, body["code"]), (HTTPStatus.FORBIDDEN, "energy_lockout")
            )
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(
            self.states("climate.majlis_ac", "climate.server_ac"),
            {"climate.majlis_ac": "cool", "climate.server_ac": "off"},
        )


class NeverAtAGuess(RoomOffAcCase):
    async def test_no_capture_means_lights_and_fans_as_ever(self):
        # Switched off at the wall: the hub has no capture of the AC.
        self.press("light.majlis_main", "on")
        self.press("climate.majlis_ac", "cool")
        self.press("climate.majlis_ac", "off")
        self.press("light.majlis_main", "off")
        rooms, _ = await self.rooms()
        self.assertEqual(
            rooms["majlis"]["rank"]["one_tap"], one_tap(1, source="last_on")
        )
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"]), (200, "last_on"))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(self.states("climate.majlis_ac"), {"climate.majlis_ac": "off"})

    async def test_a_capture_older_than_a_day_is_dropped(self):
        await self.majlis_running()
        await self.off("majlis")
        self.age_capture("majlis", hours=25)
        rooms, _ = await self.rooms()
        self.assertEqual(
            rooms["majlis"]["rank"]["one_tap"], one_tap(1, source="last_on")
        )
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"]), (200, "last_on"))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(self.now_data.restore_acs("majlis"), {})

    async def test_an_old_ac_only_capture_is_dropped_and_the_room_opens(self):
        await self.off("server")
        self.age_capture("server", hours=25)
        status, body = await self.on("server")
        self.assertEqual(
            (status, body["error"]), (HTTPStatus.CONFLICT, "nothing_to_turn_on")
        )
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(self.now_data.restore_acs("server"), {})

    async def test_a_room_lit_again_and_switched_off_at_the_wall_wins(self):
        await self.majlis_running()
        await self.off("majlis")
        self.press("light.majlis_spot", "on")
        self.press("light.majlis_spot", "off", at=datetime.now(UTC).timestamp() + 600)
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"]), (200, "last_on"))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(self.now_data.restore_acs("majlis"), {})

    async def test_a_later_off_that_found_the_ac_off_forgets_it(self):
        await self.majlis_running()
        await self.off("majlis")
        self.age_capture("majlis", minutes=10)
        self.press("light.majlis_main", "on")
        await self.off("majlis")
        self.assertEqual(self.now_data.restore_acs("majlis"), {})
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"]), (200, "capture"))
        self.assertEqual(self.ac_commands, [])

    async def test_a_target_it_no_longer_takes_leaves_it_off(self):
        await self.majlis_running()
        await self.off("majlis")
        self.hass.states.async_set("climate.majlis_ac", "off", {**AC, "min_temp": 24})
        rooms, _ = await self.rooms()
        self.assertEqual(rooms["majlis"]["rank"]["one_tap"], one_tap(1))
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"], on["ok"]), (200, "capture", True))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(self.now_data.restore_acs("majlis"), {})

    async def test_one_someone_already_switched_on_stays_as_they_set_it(self):
        await self.majlis_running()
        await self.off("majlis")
        self.hass.states.async_set(
            "climate.majlis_ac", "heat", {**AC, "temperature": 26}
        )
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, True))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(self.outcomes(on), {"light.majlis_main": "changed"})
        self.assertEqual(on["restored_count"], 2)
        self.assertEqual(
            self.states("climate.majlis_ac"), {"climate.majlis_ac": "heat"}
        )
        self.assertEqual(self.now_data.restore_acs("majlis"), {})


class WhenAnAcDoesntAnswer(RoomOffAcCase):
    async def test_one_that_refuses_the_off_is_reported_and_not_captured(self):
        await self.majlis_running()
        self.broken("set_hvac_mode")
        status, off = await self.off("majlis")
        self.assertEqual((status, off["ok"]), (200, False))
        self.assertEqual(
            self.outcomes(off),
            {"light.majlis_main": "changed", "climate.majlis_ac": "failed"},
        )
        self.assertEqual(
            self.states("climate.majlis_ac"), {"climate.majlis_ac": "cool"}
        )
        self.assertEqual(self.now_data.restore_acs("majlis"), {})
        self.assertEqual(self.now_data.restore_set("majlis"), ["light.majlis_main"])
        # Still on, the room says so, and a tap tries again.
        rooms, _ = await self.rooms()
        self.assertEqual(self.counts(rooms["majlis"]), (0, True, 1, 1))
        self.fixed("set_hvac_mode")
        status, off = await self.off("majlis")
        self.assertEqual((status, off["ok"]), (200, True))
        self.assertEqual(
            self.now_data.restore_acs("majlis"),
            {"climate.majlis_ac": {"hvac_mode": "cool", "temperature": 22}},
        )

    async def test_one_that_stays_on_after_the_call_is_reported(self):
        await self.majlis_running()

        async def ignored(call):
            return None

        self.hass.services.async_register("climate", "set_hvac_mode", ignored)
        status, off = await self.off("majlis")
        self.assertEqual((status, off["ok"]), (200, False))
        self.assertEqual(self.outcomes(off)["climate.majlis_ac"], "failed")
        self.assertEqual(self.now_data.restore_acs("majlis"), {})

    async def test_one_that_refuses_to_come_back_stays_captured(self):
        await self.majlis_running()
        await self.off("majlis")
        self.broken("set_hvac_mode")
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, False))
        self.assertEqual(
            self.outcomes(on),
            {"light.majlis_main": "changed", "climate.majlis_ac": "failed"},
        )
        self.assertEqual(on["restore_pending_count"], 1)
        self.assertEqual(self.now_data.restore_set("majlis"), [])
        self.assertEqual(
            self.now_data.restore_acs("majlis"),
            {"climate.majlis_ac": {"hvac_mode": "cool", "temperature": 22}},
        )
        self.fixed("set_hvac_mode")
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, True))
        self.assertEqual(self.ac_commands, [COOL, AT_22])
        self.assertEqual(self.now_data.restore_acs("majlis"), {})

    async def test_one_offline_is_reported_and_comes_back_later(self):
        await self.majlis_running()
        await self.off("majlis")
        self.hass.states.async_set("climate.majlis_ac", "unavailable", AC)
        rooms, _ = await self.rooms()
        self.assertEqual(rooms["majlis"]["rank"]["one_tap"], one_tap(1, unavailable=1))
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, False))
        self.assertEqual(self.ac_commands, [])
        self.assertEqual(
            self.outcomes(on),
            {"light.majlis_main": "changed", "climate.majlis_ac": "unavailable"},
        )
        self.hass.states.async_set("climate.majlis_ac", "off", AC)
        status, on = await self.on("majlis")
        self.assertEqual((status, on["ok"]), (200, True))
        self.assertEqual(self.ac_commands, [COOL, AT_22])


class OlderCaptures(RoomOffAcCase):
    async def test_a_capture_from_an_older_hub_brings_back_its_lights(self):
        self.storage.table("now_restore_sets")["majlis"] = {
            "entity_ids": ["light.majlis_main"],
            "captured_at": datetime.now(UTC).isoformat(),
        }
        status, on = await self.on("majlis")
        self.assertEqual((status, on["source"], on["ok"]), (200, "capture", True))
        self.assertEqual(self.ac_commands, [])
