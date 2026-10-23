"""The all-off room ranking against a real Home Assistant and the real log.

RankingOverTheRecorder: a real Home Assistant core (state machine, bus,
services, contexts, CoreState) drives the real ActivityRecorder over a temp
HubStorage, and room_intel.ranking reads it back through
ActivityRecorder.async_recent_events with the real filtering and registry
(the fixture of test_room_activity_recorder). NowPageOffRooms: the Now page
through the real view, with the recorder reading a filled log. Real-HA
environment only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H
from test_room_activity_recorder import RecorderTestCase

try:
    from casasmart.now_api import CasaSmartNowView
    from casasmart.now_data import NowDataEngine
    from casasmart.room_intel import activity
    from casasmart.room_intel import recorder as recorder_module
    from casasmart.room_intel.ranking import async_rank_rooms
    from casasmart.room_intel.recorder import ActivityRecorder
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import Context, CoreState
    from homeassistant.helpers import entity_registry as er

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err

_NO_POLICIES = NS(
    room_policy=lambda rid: {"participates": False, "eligible_entity_ids": []}
)


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


class _RankingCase(RecorderTestCase):
    async def rank(self, scope=None, *, member=None, now_t=None):
        """{room_id: (reason, params)} as the Now page would rank them."""
        await self.hass.async_block_till_done()
        ranked, _, _ = await async_rank_rooms(
            self.hass,
            self.registry,
            _NO_POLICIES,
            scope,
            activity=self.recorder,
            member_id=member,
            now_t=now_t,
        )
        return {
            r["room_id"]: (r["rank"]["reason"], r["rank"]["params"]) for r in ranked
        }

    async def recent(self):
        events = await self.recorder.async_recent_events(0, ["majlis", "kitchen"])
        return [(e["t"], e["entity_id"], e["action"], e["source"]) for e in events]


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class RankingOverTheRecorder(_RankingCase):
    async def test_a_wall_press_is_just_off_buffered_and_written_seen_once(self):
        self.press("light.majlis", "on")
        self.press("light.majlis", "off")
        await self.hass.async_block_till_done()
        # Not written yet: the ranking sees the recorder's buffer.
        self.assertEqual(self.storage.room_activity().count(), 0)
        buffered = await self.recent()
        self.assertEqual(
            [event[1:] for event in buffered],
            [
                ("light.majlis", "off", "wall_switch"),
                ("light.majlis", "on", "wall_switch"),
            ],
        )
        went_dark = buffered[0][0]
        expected = {
            "majlis": ("just_off", {"went_dark_at": iso(went_dark)}),
            "kitchen": ("room_order", {}),
        }
        self.assertEqual(await self.rank(), expected)
        # Written: the same two events, once each.
        await self.recorder.async_flush()
        self.assertEqual(self.storage.room_activity().count(), 2)
        self.assertEqual(await self.recent(), buffered)
        self.assertEqual(await self.rank(), expected)
        # Thirty minutes later it's back in the room order.
        self.assertEqual(
            (await self.rank(now_t=went_dark + 1801))["majlis"], ("room_order", {})
        )

    async def test_whose_off_counts(self):
        self.press("light.majlis", "on")
        app = self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        await self.call("light", "turn_off", "light.majlis", app)
        # An automation (a run context with a parent) switches the kitchen.
        run = Context(parent_id=Context().id)
        self.hass.bus.async_fire("automation_triggered", {}, context=run)
        await self.call("switch", "turn_on", "switch.kitchen_left", run)
        await self.call("switch", "turn_off", "switch.kitchen_left", run)
        for member, by_you in (
            ("mem-a", True),  # their own tap: after the habits
            ("mem-b", False),
            (None, False),
        ):
            with self.subTest(member=member):
                reasons = await self.rank(member=member)
                self.assertEqual(
                    (reasons["majlis"][0], reasons["kitchen"][0]),
                    ("just_off", "room_order"),
                )
                self.assertEqual(reasons["majlis"][1].get("by_you", False), by_you)
                self.assertNotIn("mem-", json.dumps(reasons))

    async def test_on_unavailable_off_is_not_just_off(self):
        self.press("light.majlis", "on")
        self.press("light.majlis", "unavailable")
        self.press("light.majlis", "off")  # back, off: nothing is logged
        self.assertEqual((await self.rank())["majlis"], ("room_order", {}))
        self.assertEqual(
            [event[1:3] for event in await self.recent()], [("light.majlis", "on")]
        )

    async def test_a_light_dropping_off_after_the_off_blocks_just_off(self):
        self.press("light.majlis", "on")
        self.press("light.majlis_spot", "on")
        self.press("light.majlis", "off")
        # The spot is still on, then drops off the network: the room only
        # looks dark.
        self.press("light.majlis_spot", "unavailable")
        self.assertEqual((await self.rank())["majlis"], ("room_order", {}))
        # Back as on: the room is on again.
        self.press("light.majlis_spot", "on")
        self.assertEqual((await self.rank())["majlis"], ("active", {}))
        # A real off now makes it just_off.
        self.press("light.majlis_spot", "off")
        self.assertEqual((await self.rank())["majlis"][0], "just_off")

    async def test_a_light_dead_since_before_the_off_does_not_block(self):
        # Dead since an hour ago (Home Assistant's own timestamp argument).
        self.hass.states.async_set(
            "light.majlis_spot", "unavailable", timestamp=time.time() - 3600
        )
        self.press("light.majlis", "on")
        self.press("light.majlis", "off")
        self.assertEqual((await self.rank())["majlis"][0], "just_off")

    async def test_a_scoped_member_sees_only_their_rooms_activity(self):
        self.press("switch.kitchen_left", "on")
        self.press("switch.kitchen_left", "off")
        before = await self.rank(["kitchen"], member="mem-a")
        self.press("light.majlis", "on")
        self.press("light.majlis", "off")
        self.press("media_player.majlis_tv", "playing")
        self.assertEqual(await self.rank(["kitchen"], member="mem-a"), before)
        self.assertEqual(list(before), ["kitchen"])
        self.assertEqual(before["kitchen"][0], "just_off")

    async def test_a_log_that_cant_be_read_leaves_the_room_order(self):
        self.press("light.majlis", "on")
        self.press("light.majlis", "off")
        await self.recorder.async_flush()
        failing = mock.patch.object(
            self.storage.room_activity().__class__,
            "recent_in_rooms",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        )
        with failing:
            with self.assertLogs(recorder_module._LOGGER, "WARNING") as logs:
                self.assertEqual((await self.rank())["majlis"], ("room_order", {}))
            self.assertIn("could not be read", logs.output[0])
            # Warned once, not on every Now page.
            with self.assertNoLogs(recorder_module._LOGGER, "WARNING"):
                await self.rank()
        self.assertEqual((await self.rank())["majlis"][0], "just_off")


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class RankingAfterARestart(_RankingCase):
    already_running = False

    async def start_home_assistant(self):
        """Home Assistant starts: restores, reports, started, settled."""
        self.hass.set_state(CoreState.starting)
        self.press("light.majlis", "unavailable")
        self.press("light.majlis", "on")  # restored as on
        self.hass.set_state(CoreState.running)
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done()
        await asyncio.sleep(0.1)  # past the recorder's (test) settle
        await self.hass.async_block_till_done()

    async def test_a_device_catching_up_after_a_restart_is_not_just_off(self):
        await self.start_home_assistant()
        self.assertIsNotNone(self.recorder.started_at)
        self.assertEqual((await self.rank())["majlis"], ("active", {}))
        # The light was switched off while Home Assistant was down; it
        # reports so now. The log has it, the ranking doesn't trust its time.
        self.press("light.majlis", "off")
        self.assertEqual(
            [event[1:] for event in await self.recent()],
            [("light.majlis", "off", "wall_switch")],
        )
        self.assertEqual((await self.rank())["majlis"], ("room_order", {}))

    async def test_offs_count_again_ten_minutes_after_the_start(self):
        await self.recorder.async_stop()
        self.recorder = ActivityRecorder(
            self.hass,
            self.registry,
            self.storage.room_activity(),
            startup_settle=0.05,
            # Home Assistant "started" eleven minutes ago.
            wall_clock=lambda: time.time() - 660,
        )
        await self.recorder.async_start()
        await self.start_home_assistant()
        self.press("light.majlis", "off")
        self.assertEqual((await self.rank())["majlis"][0], "just_off")

    async def test_a_reload_has_no_quiet_window(self):
        await self.recorder.async_stop()
        self.hass.set_state(CoreState.running)
        self.recorder = ActivityRecorder(
            self.hass, self.registry, self.storage.room_activity()
        )
        await self.recorder.async_start()
        self.assertIsNone(self.recorder.started_at)
        self.press("light.majlis", "on")
        self.press("light.majlis", "off")
        self.assertEqual((await self.rank())["majlis"][0], "just_off")


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class RunningLoadHysteresis(_RankingCase):
    """A meter that crossed 20 W keeps its room running until below 14 W."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The kitchen switch's device reports its power (a 1PM-style relay).
        self.registry.assign_device("sensor.kitchen_power", room_id="kitchen")
        record = dict(self.storage.table("registry_user_devices")["dev-kitchen"])
        record["config_entity_ids"] = [
            *record["config_entity_ids"],
            "sensor.kitchen_power",
        ]
        self.storage.table("registry_user_devices")["dev-kitchen"] = record
        await self.registry_changed()

    async def registry_changed(self):
        with mock.patch.object(recorder_module, "_REGISTRY_DEBOUNCE_SECONDS", 0):
            self.hass.bus.async_fire("casasmart_registry_changed", {"kind": "devices"})
            await self.hass.async_block_till_done()
            await asyncio.sleep(0.01)
            await self.hass.async_block_till_done()

    def read(self, watts):
        self.hass.states.async_set(
            "sensor.kitchen_power",
            watts,
            {"device_class": "power", "unit_of_measurement": "W"},
        )

    async def kitchen(self):
        return (await self.rank())["kitchen"][0]

    async def test_a_load_that_crossed_20_w_runs_until_it_drops_below_14_w(self):
        for watts, reason in (
            ("16", "room_order"),
            ("25", "running_load"),
            ("16", "running_load"),
            ("14", "running_load"),
            ("13", "room_order"),
            ("16", "room_order"),
            ("20", "running_load"),
            ("unavailable", "room_order"),
            ("16", "room_order"),
        ):
            with self.subTest(watts=watts):
                self.read(watts)
                self.assertEqual(await self.kitchen(), reason)
        # Readings are not room activity.
        self.assertEqual(await self.rows(), [])

    async def test_after_a_restart_a_meter_holds_from_its_first_change(self):
        self.read("25")
        await self.hass.async_block_till_done()
        await self.recorder.async_stop()
        self.recorder = ActivityRecorder(
            self.hass, self.registry, self.storage.room_activity()
        )
        await self.recorder.async_start()
        # Never seen: 25 W runs on its own reading.
        self.assertEqual(await self.kitchen(), "running_load")
        # The first change counts the reading before it.
        self.read("16")
        self.assertEqual(await self.kitchen(), "running_load")

        # Restarted again at 16 W: nothing holds it.
        await self.recorder.async_stop()
        self.recorder = ActivityRecorder(
            self.hass, self.registry, self.storage.room_activity()
        )
        await self.recorder.async_start()
        self.read("17")
        self.assertEqual(await self.kitchen(), "room_order")

    async def test_a_meter_no_longer_on_a_hub_device_is_let_go(self):
        self.read("25")
        self.read("16")
        self.assertEqual(await self.kitchen(), "running_load")
        self.assertEqual(self.recorder.held_meters(), {"sensor.kitchen_power"})
        record = dict(self.storage.table("registry_user_devices")["dev-kitchen"])
        record["config_entity_ids"] = ["switch.kitchen_lock"]
        self.storage.table("registry_user_devices")["dev-kitchen"] = record
        await self.registry_changed()
        self.assertEqual(self.recorder.held_meters(), frozenset())


class _NoEntityRegistry:
    def async_get(self, entity_id):
        return None


ROOMS = {
    "room-majlis": {"name": "Majlis", "floor_id": None, "icon": None, "sort_order": 0},
    "room-kitchen": {"name": "Kitchen", "floor_id": None, "icon": None, "sort_order": 1},
    "room-bedroom": {"name": "Bedroom", "floor_id": None, "icon": None, "sort_order": 2},
    "room-office": {"name": "Office", "floor_id": None, "icon": None, "sort_order": 3},
}  # fmt: skip
DEVICES = {
    "dev-majlis": ("room-majlis", [("light.majlis", "off"), ("media_player.majlis_tv", "playing")]),
    "dev-kitchen": ("room-kitchen", [("light.kitchen", "off")]),
    "dev-bedroom": ("room-bedroom", [("light.bedroom", "off")]),
    "dev-office": ("room-office", [("light.office", "off")]),
}  # fmt: skip


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class NowPageOffRooms(unittest.IsolatedAsyncioTestCase):
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
            for eid, state in entities:
                storage.table("registry_devices")[eid] = {
                    "room_id": room_id,
                    "display_name": None,
                    "sort_order": 0,
                }
                self.hass.states.add(eid, state=state)
            storage.table("registry_user_devices")[device_id] = {
                "entity_ids": [eid for eid, _ in entities]
            }
        self.runtime.registry.warm_up()
        self.runtime.now_data = NowDataEngine(
            storage.table("now_recents"),
            storage.table("now_room_policies"),
            storage.table("now_config"),
            storage.table("now_restore_sets"),
            storage.table("now_idempotency"),
        )
        self.runtime.energy = None
        self.runtime.suggestions = None
        # The recorder, not started: the view only reads the log through it.
        self.runtime.room_activity = ActivityRecorder(
            self.hass, self.runtime.registry, storage.room_activity()
        )
        now = int(time.time())
        self.kitchen_dark = now - 4 * 60
        self.bedroom_dark = now - 60
        storage.room_activity().append_many(
            [
                self.event("room-kitchen", "light.kitchen", "on", now - 3600),
                self.event("room-kitchen", "light.kitchen", "off", self.kitchen_dark),
                self.event("room-bedroom", "light.bedroom", "on", now - 3600),
                self.event(
                    "room-bedroom",
                    "light.bedroom",
                    "off",
                    self.bedroom_dark,
                    source="app",
                    member_id="mem-owner",
                ),
            ]
        )
        self.view = CasaSmartNowView(self.hass)

    @staticmethod
    def event(room_id, entity_id, action, t, source="wall_switch", member_id=None):
        return {
            "t": t,
            "utc_offset_min": 180,
            "room_id": room_id,
            "entity_id": entity_id,
            "action": action,
            "source": source,
            "member_id": member_id,
        }

    async def room_activity(self, *, member_id, rooms=None, role="admin", query=None):
        _, headers = H.session(
            self.runtime.auth, role=role, rooms=rooms, member_id=member_id
        )
        status, body = H.read_response(
            await self.view.get(H.FakeRequest(headers=headers, query=query))
        )
        self.assertEqual(status, 200)
        return body["room_activity"]

    async def test_the_now_page_sends_each_off_rooms_reason(self):
        activity_block = await self.room_activity(member_id="mem-other")
        self.assertEqual(
            activity_block["ranking"],
            {
                "version": 1,
                "basis": "count",
                "slots": 5,
                "mode": "all_off",
                # No habit learner on this hub.
                "habits": {"layer": "personal", "status": "unavailable"},
            },
        )
        self.assertEqual(
            [
                (r["room_id"], r["rank"]["reason"], r["rank"]["params"])
                for r in activity_block["rooms"]
            ],
            [
                ("room-majlis", "running_load", {"loads": ["media"]}),
                ("room-bedroom", "just_off", {"went_dark_at": iso(self.bedroom_dark)}),
                ("room-kitchen", "just_off", {"went_dark_at": iso(self.kitchen_dark)}),
                ("room-office", "room_order", {}),
            ],
        )  # fmt: skip
        self.assertTrue(all(r["rank"]["filler"] for r in activity_block["rooms"]))
        self.assertNotIn("mem-", json.dumps(activity_block))

    async def test_the_members_own_off_comes_after_everyone_elses(self):
        # Still just_off, marked by_you, below the habits (none here) and
        # above the room order.
        rooms = (await self.room_activity(member_id="mem-owner"))["rooms"]
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["params"]) for r in rooms],
            [
                ("room-majlis", "running_load", {"loads": ["media"]}),
                ("room-kitchen", "just_off", {"went_dark_at": iso(self.kitchen_dark)}),
                ("room-bedroom", "just_off",
                 {"went_dark_at": iso(self.bedroom_dark), "by_you": True}),
                ("room-office", "room_order", {}),
            ],
        )  # fmt: skip

    async def test_the_now_page_reads_the_rooms_and_policies_once(self):
        registry, now_data = self.runtime.registry, self.runtime.now_data
        with (
            mock.patch.object(
                registry, "list_rooms", wraps=registry.list_rooms
            ) as rooms,
            mock.patch.object(
                registry, "list_user_devices", wraps=registry.list_user_devices
            ) as devices,
            mock.patch.object(
                now_data, "room_policy", wraps=now_data.room_policy
            ) as policy,
        ):
            await self.room_activity(member_id="mem-other")
        self.assertEqual((rooms.call_count, devices.call_count), (1, 1))
        # One policy read per room (four rooms here).
        self.assertEqual(policy.call_count, 4)

    async def test_the_shared_tablet_has_no_own_offs(self):
        # ?habits=household (the wall tablet, its own member): an off made
        # from it is an off like anyone's, never "You turned this off".
        rooms = (
            await self.room_activity(
                member_id="mem-owner", query={"habits": "household"}
            )
        )["rooms"]
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["params"]) for r in rooms],
            [
                ("room-majlis", "running_load", {"loads": ["media"]}),
                ("room-bedroom", "just_off", {"went_dark_at": iso(self.bedroom_dark)}),
                ("room-kitchen", "just_off", {"went_dark_at": iso(self.kitchen_dark)}),
                ("room-office", "room_order", {}),
            ],
        )  # fmt: skip

    async def test_a_scoped_member_gets_only_their_rooms(self):
        rooms = (
            await self.room_activity(
                member_id="mem-other",
                rooms=["room-office", "room-kitchen"],
                role="user",
            )
        )["rooms"]
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"]) for r in rooms],
            [("room-kitchen", "just_off"), ("room-office", "room_order")],
        )


if __name__ == "__main__":
    unittest.main()
