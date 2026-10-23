"""One tap "on" end to end, against a real Home Assistant.

POST /api/casasmart/now/rooms/{room_id}/activity and GET /api/casasmart/now
through the real views, over a real HomeAssistant core (bus, state machine,
services, Context), the real ActivityRecorder keeping the last-on sets, the
real NowDataEngine, RegistryEngine and AuthEngine over a temp HubStorage. The
fixture is test_room_last_on_ha's; only Home Assistant's entity registry and
the config-entry lookup are fakes. The service handlers record every call, so
a test sees exactly what the hub switched. Real-HA environment only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H
from test_room_last_on_ha import IMPORT_ERROR, LastOnHubCase

try:
    from casasmart.auth_engine import AuthEngine
    from casasmart.now_api import CasaSmartNowView, CasaSmartRoomActivityCommandView
    from casasmart.now_data import NowDataEngine
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import Context, CoreState
    from homeassistant.exceptions import HomeAssistantError
except Exception as err:
    IMPORT_ERROR = IMPORT_ERROR or err

MAJLIS_POLICY = [
    "light.majlis_main",
    "light.majlis_spot",
    "fan.majlis",
    "switch.majlis_relay",
    "switch.majlis_outlet",
]
KITCHEN_POLICY = ["light.kitchen", "fan.kitchen_hood", "switch.kitchen_heater"]


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class OneTapCase(LastOnHubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        storage = self.storage
        self.runtime.auth = AuthEngine(storage.table("auth_devices"), H.FakeHubConfig())
        self.now_data = NowDataEngine(
            storage.table("now_recents"),
            storage.table("now_room_policies"),
            storage.table("now_config"),
            storage.table("now_restore_sets"),
            storage.table("now_idempotency"),
            start_profiles_table=storage.table("now_room_start_profiles"),
        )
        self.runtime.now_data = self.now_data
        self.runtime.energy = None
        self.runtime.suggestions = None
        self.runtime.room_habits = None
        self.now_data.set_room_policy("majlis", True, MAJLIS_POLICY)
        self.now_data.set_room_policy("kitchen", True, KITCHEN_POLICY)
        self.now_data.set_room_policy("bedroom", True, ["light.bedroom"])
        # Anything the hub might wrongly switch is recorded too.
        for domain, services in (
            ("climate", ("turn_on", "turn_off", "set_hvac_mode")),
            ("cover", ("open_cover", "close_cover")),
            ("media_player", ("turn_on", "turn_off")),
        ):
            for service in services:
                self.hass.services.async_register(domain, service, self._record)
        self._keys = 0
        _, self.headers = H.session(self.runtime.auth, role="user", member_id="mem-a")

    async def _record(self, call):
        ids = call.data["entity_id"]
        self.calls.append(
            (call.domain, call.service, tuple([ids] if isinstance(ids, str) else ids))
        )

    async def command(self, room_id, action, *, headers=None):
        """(status, body) of a room command, with a new idempotency key."""
        await self.hass.async_block_till_done()
        self._keys += 1
        view = CasaSmartRoomActivityCommandView(self.hass)
        status, body = H.read_response(
            await view.post(
                H.FakeRequest(
                    headers=headers or self.headers,
                    body={"action": action, "idempotency_key": f"key-{self._keys:04d}"},
                ),
                room_id,
            )
        )
        await self.hass.async_block_till_done()
        return status, body

    async def turn_on(self, room_id, **kwargs):
        self.calls.clear()
        return await self.command(room_id, "turn_on", **kwargs)

    async def now_rooms(self, headers=None):
        """{room_id: (restore_pending_count, rank.one_tap)} from the Now page."""
        await self.hass.async_block_till_done()
        status, body = H.read_response(
            await CasaSmartNowView(self.hass).get(
                H.FakeRequest(headers=headers or self.headers)
            )
        )
        self.assertEqual(status, 200)
        return {
            room["room_id"]: (room["restore_pending_count"], room["rank"]["one_tap"])
            for room in body["room_activity"]["rooms"]
        }

    def states(self, *entity_ids):
        return {eid: self.hass.states.get(eid).state for eid in entity_ids}

    def switched(self):
        """The domains and services the hub called, in order."""
        return [(domain, service) for domain, service, _ in self.calls]


def last_on(lights, fans=0, unavailable=0):
    return {
        "action": "turn_on",
        "source": "last_on",
        "light_count": lights,
        "fan_count": fans,
        "unavailable_count": unavailable,
    }


class OneTapFromAnyOff(OneTapCase):
    async def test_a_room_switched_off_at_the_wall_turns_back_on(self):
        for eid in ("light.majlis_main", "light.majlis_spot", "fan.majlis"):
            self.press(eid, "on")
        for eid in ("light.majlis_spot", "fan.majlis", "light.majlis_main"):
            self.press(eid, "off")
        # This room has no capture: once a 409, "Couldn't turn on Majlis".
        self.assertEqual((await self.now_rooms())["majlis"], (3, last_on(2, 1)))

        status, body = await self.turn_on("majlis")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "last_on")
        self.assertTrue(body["ok"])
        self.assertEqual(
            body["outcomes"],
            [
                {"entity_id": "light.majlis_main", "outcome": "changed"},
                {"entity_id": "fan.majlis", "outcome": "changed"},
                {"entity_id": "light.majlis_spot", "outcome": "changed"},
            ],
        )
        self.assertEqual(body["restored_count"], 3)
        self.assertEqual(body["restored_from_capture_count"], 0)
        self.assertEqual(body["restore_pending_count"], 0)
        self.assertEqual(
            self.calls,
            [
                ("light", "turn_on", ("light.majlis_main", "light.majlis_spot")),
                ("fan", "turn_on", ("fan.majlis",)),
            ],
        )
        self.assertEqual(
            set(
                self.states(
                    "light.majlis_main", "light.majlis_spot", "fan.majlis"
                ).values()
            ),
            {"on"},
        )
        # The member's own tap: logged as theirs, like any room command.
        await self.recorder.async_flush()
        rows = self.storage.room_activity().events(limit=10)
        self.assertEqual(
            {(r["entity_id"], r["source"], r["member_id"]) for r in rows[:3]},
            {
                ("light.majlis_main", "app", "mem-a"),
                ("light.majlis_spot", "app", "mem-a"),
                ("fan.majlis", "app", "mem-a"),
            },
        )

    async def test_a_room_an_automation_switched_off_turns_back_on(self):
        self.press("light.kitchen", "on")
        self.press("fan.kitchen_hood", "on")
        run = Context(parent_id=Context().id)
        self.hass.bus.async_fire("automation_triggered", {}, context=run)
        await self.call("light", "turn_off", ["light.kitchen"], run)
        await self.call("fan", "turn_off", ["fan.kitchen_hood"], run)

        status, body = await self.turn_on("kitchen")
        self.assertEqual((status, body["source"], body["ok"]), (200, "last_on", True))
        self.assertEqual(
            self.states("light.kitchen", "fan.kitchen_hood"),
            {"light.kitchen": "on", "fan.kitchen_hood": "on"},
        )

    async def test_a_room_a_home_assistant_user_switched_off(self):
        self.press("light.bedroom", "on")
        await self.call(
            "light", "turn_off", ["light.bedroom"], Context(user_id="ha-user")
        )
        status, body = await self.turn_on("bedroom")
        self.assertEqual((status, body["source"]), (200, "last_on"))
        self.assertEqual(self.states("light.bedroom"), {"light.bedroom": "on"})


class OneTapNeverTouches(OneTapCase):
    async def test_acs_plugs_relays_covers_heaters_and_tvs(self):
        on = {
            "light.majlis_main": "on",
            "switch.majlis_relay": "on",
            "switch.majlis_outlet": "on",
            "climate.majlis_ac": "cool",
            "cover.majlis_curtain": "open",
            "media_player.majlis_tv": "playing",
        }
        for eid, state in on.items():
            self.press(eid, state)
        off = {
            "climate.majlis_ac": "off",
            "cover.majlis_curtain": "closed",
            "media_player.majlis_tv": "off",
            "switch.majlis_outlet": "off",
            "switch.majlis_relay": "off",
            "light.majlis_main": "off",
        }
        for eid, state in off.items():
            self.press(eid, state)
        status, body = await self.turn_on("majlis")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [("light", "turn_on", ("light.majlis_main",))])
        self.assertEqual(self.states(*off), {**off, "light.majlis_main": "on"})

    async def test_not_even_from_the_hubs_own_capture(self):
        # The room's policy approves the relay and the outlet; a room off
        # switches them off and captures them; on brings back only the light.
        for eid in ("light.majlis_main", "switch.majlis_relay", "switch.majlis_outlet"):
            self.press(eid, "on")
        status, off = await self.command("majlis", "turn_off")
        self.assertEqual((status, off["restore_pending_count"]), (200, 3))
        self.assertEqual(
            self.now_data.restore_set("majlis"),
            ["light.majlis_main", "switch.majlis_relay", "switch.majlis_outlet"],
        )
        self.assertEqual(
            (await self.now_rooms())["majlis"],
            (1, {**last_on(1), "source": "capture"}),
        )
        status, body = await self.turn_on("majlis")
        self.assertEqual((status, body["source"], body["ok"]), (200, "capture", True))
        self.assertEqual(self.calls, [("light", "turn_on", ("light.majlis_main",))])
        self.assertEqual(
            self.states("switch.majlis_relay", "switch.majlis_outlet"),
            {"switch.majlis_relay": "off", "switch.majlis_outlet": "off"},
        )
        # The capture is done; the switches don't linger in it.
        self.assertEqual(self.now_data.restore_set("majlis"), [])

    async def test_a_heater_relay_in_the_kitchen(self):
        self.press("switch.kitchen_heater", "on")
        self.press("light.kitchen", "on")
        self.press("light.kitchen", "off")
        self.press("switch.kitchen_heater", "off")
        status, _ = await self.turn_on("kitchen")
        self.assertEqual(status, 200)
        self.assertEqual(self.switched(), [("light", "turn_on")])
        self.assertEqual(
            self.states("switch.kitchen_heater"), {"switch.kitchen_heater": "off"}
        )


class OneTapSkips(OneTapCase):
    async def dark_majlis(self):
        for eid in ("light.majlis_main", "light.majlis_spot", "fan.majlis"):
            self.press(eid, "on")
        for eid in ("fan.majlis", "light.majlis_spot", "light.majlis_main"):
            self.press(eid, "off")
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.last_on("majlis"),
            ["light.majlis_main", "light.majlis_spot", "fan.majlis"],
        )

    async def test_a_deleted_light_and_a_moved_fan_are_left_out(self):
        await self.dark_majlis()
        self.hass.states.async_remove("light.majlis_spot")
        self.registry.assign_device("fan.majlis", room_id="bedroom")
        self.assertEqual((await self.now_rooms())["majlis"], (1, last_on(1)))
        status, body = await self.turn_on("majlis")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(
            body["outcomes"], [{"entity_id": "light.majlis_main", "outcome": "changed"}]
        )
        self.assertEqual(self.calls, [("light", "turn_on", ("light.majlis_main",))])
        self.assertEqual(self.states("fan.majlis"), {"fan.majlis": "off"})

    async def test_an_unavailable_light_is_reported_not_switched(self):
        await self.dark_majlis()
        self.press("light.majlis_spot", "unavailable")
        self.assertEqual(
            (await self.now_rooms())["majlis"], (2, last_on(1, 1, unavailable=1))
        )
        status, body = await self.turn_on("majlis")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(
            body["outcomes"],
            [
                {"entity_id": "light.majlis_main", "outcome": "changed"},
                {"entity_id": "fan.majlis", "outcome": "changed"},
                {"entity_id": "light.majlis_spot", "outcome": "unavailable"},
            ],
        )
        self.assertEqual(
            (body["restored_count"], body["restore_pending_count"]), (2, 1)
        )
        self.assertNotIn("light.majlis_spot", str(self.calls))

    async def test_a_room_whose_lights_are_all_unavailable_opens(self):
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "off")
        self.press("light.bedroom", "unavailable")
        self.assertEqual(
            (await self.now_rooms())["bedroom"],
            (0, {"action": "open_room", "reason": "unavailable"}),
        )
        status, body = await self.turn_on("bedroom")
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(
            (body["error"], body["reason"], body["open_room"]),
            ("nothing_to_turn_on", "unavailable", True),
        )
        self.assertEqual(self.calls, [])

    async def test_a_light_that_fails_is_reported(self):
        await self.dark_majlis()

        async def broken(call):
            raise HomeAssistantError("fan offline")

        self.hass.services.async_register("fan", "turn_on", broken)
        status, body = await self.turn_on("majlis")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn(
            {"entity_id": "fan.majlis", "outcome": "failed"}, body["outcomes"]
        )
        self.assertEqual(body["restore_pending_count"], 1)


class OneTapPriority(OneTapCase):
    async def test_the_hubs_capture_beats_the_last_on_set(self):
        # Off, someone turns the hood back on, off again: the capture holds
        # both, the last-on set only the hood.
        self.press("light.kitchen", "on")
        self.press("fan.kitchen_hood", "on")
        await self.command("kitchen", "turn_off")
        self.press("fan.kitchen_hood", "on")
        await self.command("kitchen", "turn_off")
        self.assertEqual(self.last_on("kitchen"), ["fan.kitchen_hood"])
        self.assertEqual(
            self.now_data.restore_set("kitchen"), ["light.kitchen", "fan.kitchen_hood"]
        )
        status, body = await self.turn_on("kitchen")
        self.assertEqual((status, body["source"]), (200, "capture"))
        self.assertEqual(body["restored_from_capture_count"], 2)
        self.assertEqual(
            self.states("light.kitchen", "fan.kitchen_hood"),
            {"light.kitchen": "on", "fan.kitchen_hood": "on"},
        )
        self.assertEqual(self.now_data.restore_set("kitchen"), [])

    async def test_a_capture_older_than_a_day_gives_way_and_goes(self):
        self.press("light.kitchen", "on")
        self.press("fan.kitchen_hood", "on")
        await self.command("kitchen", "turn_off")
        record = self.storage.table("now_restore_sets")["kitchen"]
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        self.storage.table("now_restore_sets")["kitchen"] = {
            **record,
            "captured_at": old,
        }
        # With no last-on set either (dropped here), the start profile is next.
        self.recorder.last_on.clear()
        self.now_data.set_start_profile("kitchen", ["light.kitchen"])
        status, body = await self.turn_on("kitchen")
        self.assertEqual((status, body["source"]), (200, "start_profile"))
        self.assertEqual(self.now_data.restore_set("kitchen"), [])

    async def test_a_room_lit_again_and_switched_off_at_the_wall_wins(self):
        self.press("light.kitchen", "on")
        self.press("fan.kitchen_hood", "on")
        await self.command("kitchen", "turn_off")
        # Later: only the light, on and off at the wall. The capture (both)
        # is no longer what the room was.
        self.press("light.kitchen", "on")
        self.press(
            "light.kitchen",
            "off",
            at=datetime.now(UTC).timestamp() + 600,
        )
        status, body = await self.turn_on("kitchen")
        self.assertEqual((status, body["source"]), (200, "last_on"))
        self.assertEqual(self.calls, [("light", "turn_on", ("light.kitchen",))])
        self.assertEqual(self.now_data.restore_set("kitchen"), [])

    async def test_the_start_profile_when_nothing_else_is_known(self):
        self.now_data.set_start_profile("bedroom", ["light.bedroom"])
        self.assertEqual(
            (await self.now_rooms())["bedroom"],
            (1, {**last_on(1), "source": "start_profile"}),
        )
        status, body = await self.turn_on("bedroom")
        self.assertEqual(
            (status, body["source"], body["ok"]), (200, "start_profile", True)
        )
        self.assertEqual(self.states("light.bedroom"), {"light.bedroom": "on"})

    async def test_the_last_on_set_beats_the_start_profile(self):
        self.now_data.set_start_profile("majlis", ["fan.majlis"])
        self.press("light.majlis_spot", "on")
        self.press("light.majlis_spot", "off")
        status, body = await self.turn_on("majlis")
        self.assertEqual((status, body["source"]), (200, "last_on"))
        self.assertEqual(self.calls, [("light", "turn_on", ("light.majlis_spot",))])

    async def test_a_start_profile_device_the_policy_doesnt_approve_stays_off(self):
        self.now_data.set_room_policy("majlis", True, ["light.majlis_main"])
        self.now_data.set_start_profile("majlis", ["light.majlis_main", "fan.majlis"])
        status, body = await self.turn_on("majlis")
        self.assertEqual((status, body["source"]), (200, "start_profile"))
        self.assertEqual(self.calls, [("light", "turn_on", ("light.majlis_main",))])


class OneTapNothingToTurnOn(OneTapCase):
    async def test_nothing_known_opens_the_room(self):
        self.assertEqual(
            (await self.now_rooms())["bedroom"],
            (0, {"action": "open_room", "reason": "nothing_saved"}),
        )
        status, body = await self.turn_on("bedroom")
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(
            body,
            {
                "error": "nothing_to_turn_on",
                "reason": "nothing_saved",
                "message": "Nothing to turn on: the hub doesn't know which lights "
                "and fans this room had on, and it has no start profile",
                "open_room": True,
            },
        )
        self.assertEqual(self.calls, [])
        # No "ok": true with nothing done, and nothing kept for a replay.
        self.assertNotIn("ok", body)
        self.assertEqual(dict(self.storage.table("now_idempotency").items()), {})

    async def test_room_command_rules_are_unchanged(self):
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "off")
        # A room that doesn't take part in room commands: refused as before,
        # and the Now page says the tap opens it.
        self.now_data.set_room_policy("bedroom", False, ["light.bedroom"])
        self.now_data.set_room_policy("kitchen", False, [])
        self.now_data.set_room_policy("majlis", False, [])
        status, body = await self.turn_on("bedroom")
        self.assertEqual(
            (status, body["error"]), (HTTPStatus.CONFLICT, "room_activity_disabled")
        )
        self.assertEqual(
            (await self.now_rooms())["bedroom"],
            (0, {"action": "open_room", "reason": "room_activity_disabled"}),
        )
        # A member scoped to other rooms can't reach it.
        self.now_data.set_room_policy("bedroom", True, ["light.bedroom"])
        _, scoped = H.session(
            self.runtime.auth, role="user", rooms=["kitchen"], member_id="mem-b"
        )
        status, _ = await self.turn_on("bedroom", headers=scoped)
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(self.calls, [])
        self.assertNotIn("bedroom", await self.now_rooms(headers=scoped))


class OneTapAfterARestart(OneTapCase):
    already_running = False
    settle_seconds = 0.3

    async def test_the_set_from_before_the_restart_is_what_comes_on(self):
        saved = {
            "v": 1,
            "entity_ids": ["light.majlis_main", "light.majlis_spot"],
            "at": 1_700_000_000,
        }
        await self.recorder.async_stop()
        self.storage.table("now_room_last_on")["majlis"] = saved
        self.recorder = self.new_recorder()
        await self.recorder.async_start()
        self.runtime.room_activity = self.recorder
        # The startup flood.
        self.hass.set_state(CoreState.starting)
        self.press("light.majlis_main", "unavailable")
        self.press("light.majlis_main", "on")
        self.press("light.majlis_spot", "unavailable")
        self.hass.set_state(CoreState.running)
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done()
        self.press("light.majlis_main", "off")
        self.press("light.majlis_spot", "off")
        await asyncio.sleep(self.settle_seconds + 0.1)
        await self.hass.async_block_till_done()

        status, body = await self.turn_on("majlis")
        self.assertEqual((status, body["source"]), (200, "last_on"))
        self.assertEqual(
            self.calls,
            [("light", "turn_on", ("light.majlis_main", "light.majlis_spot"))],
        )
        self.assertEqual(
            dict(self.storage.table("now_room_last_on").items()), {"majlis": saved}
        )


if __name__ == "__main__":
    unittest.main()
