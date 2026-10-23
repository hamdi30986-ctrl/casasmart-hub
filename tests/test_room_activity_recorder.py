"""The room activity recorder against a real Home Assistant core.

A real HomeAssistant instance (event bus, state machine, service registry,
Context, CoreState) drives the real ActivityRecorder, RegistryEngine and a temp
HubStorage. The test services write the new state in the call's context, which
is what Home Assistant's Entity does for 5 s after a service call
(helpers/entity.py: async_set_context). Only Home Assistant's entity registry
and the config-entry lookup are fakes. Real-HA environment only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.registry import RegistryEngine
    from casasmart.room_intel import activity
    from casasmart.room_intel import recorder as recorder_module
    from casasmart.room_intel.recorder import ActivityRecorder
    from casasmart.storage import HubStorage
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import Context, CoreState, HomeAssistant
    from homeassistant.helpers import entity_registry as er

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


class _NoEntityRegistry:
    """HA's entity registry with no entries: every entity is visible."""

    def async_get(self, entity_id):
        return None


ROOMS = {
    "majlis": {"name": "Majlis", "floor_id": None, "icon": None, "sort_order": 0},
    "kitchen": {"name": "Kitchen", "floor_id": None, "icon": None, "sort_order": 1},
}
ASSIGNMENTS = {
    "light.majlis": "majlis",
    "light.majlis_spot": "majlis",
    "climate.majlis_ac": "majlis",
    "cover.majlis_curtain": "majlis",
    "media_player.majlis_tv": "majlis",
    "switch.kitchen_left": "kitchen",
    "switch.kitchen_right": "kitchen",
    "switch.kitchen_lock": "kitchen",
    "light.kitchen_unimported": "kitchen",
    "light.hall": "hall",  # an HA area that isn't a registry room
}
USER_DEVICES = {
    "dev-majlis": {
        "entity_ids": [
            "light.majlis",
            "light.majlis_spot",
            "climate.majlis_ac",
            "cover.majlis_curtain",
            "media_player.majlis_tv",
            "light.hall",
            "light.nowhere",
        ]
    },
    "dev-kitchen": {
        "entity_ids": ["switch.kitchen_left", "switch.kitchen_right"],
        "config_entity_ids": ["switch.kitchen_lock"],
        "gangs": {
            "switch.kitchen_left": {"type": "light"},
            "switch.kitchen_right": {"type": "light", "presentation": "hidden"},
        },
    },
}
INITIAL = {
    "light.majlis": "off",
    "light.majlis_spot": "off",
    "climate.majlis_ac": "off",
    "cover.majlis_curtain": "closed",
    "media_player.majlis_tv": "off",
    "switch.kitchen_left": "off",
    "switch.kitchen_right": "off",
    "switch.kitchen_lock": "off",
    "light.kitchen_unimported": "off",
    "light.hall": "off",
    "light.nowhere": "off",
}
_SERVICE_STATES = {
    "turn_on": "on",
    "turn_off": "off",
    "open_cover": "open",
    "close_cover": "closed",
}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class RecorderTestCase(unittest.IsolatedAsyncioTestCase):
    """A hub over a real Home Assistant core, recording from the start."""

    already_running = True

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        for room_id, record in ROOMS.items():
            self.storage.table("registry_rooms")[room_id] = record
        for entity_id, room_id in ASSIGNMENTS.items():
            self.storage.table("registry_devices")[entity_id] = {
                "room_id": room_id,
                "display_name": None,
                "sort_order": 0,
            }
        for device_id, record in USER_DEVICES.items():
            self.storage.table("registry_user_devices")[device_id] = record
        self.registry = RegistryEngine(
            *(
                self.storage.table(name)
                for name in (
                    "registry_floors",
                    "registry_rooms",
                    "registry_devices",
                    "registry_scenes",
                    "registry_favorites",
                    "registry_user_devices",
                    "registry_room_tags",
                )
            )
        )
        self.registry.warm_up()

        self.hass = HomeAssistant(self._tmp.name)
        self.hass.config.time_zone = "Asia/Riyadh"
        self.hass.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        runtime = types.SimpleNamespace(registry=self.registry)
        entry = types.SimpleNamespace(runtime_data=runtime)
        self.hass.config_entries = types.SimpleNamespace(
            async_loaded_entries=lambda domain: [entry]
        )
        for domain in ("light", "switch", "cover"):
            for service in _SERVICE_STATES:
                self.hass.services.async_register(domain, service, self._handle)
        self.hass.services.async_register("climate", "set_hvac_mode", self._handle_hvac)
        for entity_id, state in INITIAL.items():
            self.hass.states.async_set(entity_id, state)
        await self.hass.async_block_till_done()
        if self.already_running:
            self.hass.set_state(CoreState.running)

        self.recorder = ActivityRecorder(
            self.hass, self.registry, self.storage.room_activity(), startup_settle=0.05
        )
        await self.recorder.async_start()
        runtime.room_activity = self.recorder

    async def asyncTearDown(self):
        await self.recorder.async_stop()
        await self.hass.async_block_till_done()

    # -- what an entity does on a service call -------------------------------

    async def _handle(self, call):
        state = _SERVICE_STATES[call.service]
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            self.hass.states.async_set(entity_id, state, context=call.context)

    async def _handle_hvac(self, call):
        self.hass.states.async_set(
            call.data["entity_id"], call.data["hvac_mode"], context=call.context
        )

    async def call(self, domain, service, entity_id, context=None, **data):
        await self.hass.services.async_call(
            domain,
            service,
            {"entity_id": entity_id, **data},
            blocking=True,
            context=context,
        )
        await self.hass.async_block_till_done()

    def press(self, entity_id, state, attributes=None):
        """A device reporting a change by itself (a wall switch)."""
        self.hass.states.async_set(entity_id, state, attributes)

    async def rows(self):
        await self.hass.async_block_till_done()
        await self.recorder.async_flush()
        events = self.storage.room_activity().events(limit=1000)
        return [
            (e["entity_id"], e["action"], e["source"], e["member_id"], e["room_id"])
            for e in reversed(events)
        ]


class SourceTests(RecorderTestCase):
    async def test_every_source_is_told_apart(self):
        app = self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        await self.call("light", "turn_on", "light.majlis", app)

        scene = self.recorder.command_context(activity.SOURCE_SCENE, "mem-b")
        await self.call("light", "turn_off", "light.majlis", scene)

        energy = self.recorder.command_context(activity.SOURCE_AUTOMATION)
        await self.call("switch", "turn_on", "switch.kitchen_left", energy)

        # An automation triggered by a state change: Home Assistant runs it in
        # a new context whose parent is the trigger's.
        trigger = Context()
        run = Context(parent_id=trigger.id)
        self.hass.bus.async_fire("automation_triggered", {}, context=run)
        await self.call("switch", "turn_off", "switch.kitchen_left", run)

        # A time-triggered automation has no parent; automation_triggered
        # names its context.
        timed = Context()
        self.hass.bus.async_fire("automation_triggered", {}, context=timed)
        await self.call("cover", "open_cover", "cover.majlis_curtain", timed)

        # A script a script_started event announced (run by an integration).
        script = Context()
        self.hass.bus.async_fire("script_started", {}, context=script)
        await self.call("cover", "close_cover", "cover.majlis_curtain", script)

        # A person in Home Assistant's own UI.
        await self.call(
            "light", "turn_on", "light.majlis", Context(user_id="ha-user-1")
        )

        # Another integration calling a service without a user (HomeKit).
        await self.call("light", "turn_off", "light.majlis")

        # The wall switch.
        self.press("light.majlis", "on")

        self.assertEqual(
            await self.rows(),
            [
                ("light.majlis", "on", "app", "mem-a", "majlis"),
                ("light.majlis", "off", "scene", "mem-b", "majlis"),
                ("switch.kitchen_left", "on", "automation", None, "kitchen"),
                ("switch.kitchen_left", "off", "automation", None, "kitchen"),
                ("cover.majlis_curtain", "open", "automation", None, "majlis"),
                ("cover.majlis_curtain", "close", "automation", None, "majlis"),
                ("light.majlis", "on", "ha_user", None, "majlis"),
                ("light.majlis", "off", "unknown", None, "majlis"),
                ("light.majlis", "on", "wall_switch", None, "majlis"),
            ],
        )

    async def test_a_room_command_credits_every_device_it_switched(self):
        app = self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        await self.call("light", "turn_on", ["light.majlis", "light.majlis_spot"], app)
        self.assertEqual(
            await self.rows(),
            [
                ("light.majlis", "on", "app", "mem-a", "majlis"),
                ("light.majlis_spot", "on", "app", "mem-a", "majlis"),
            ],
        )

    async def test_a_command_that_changes_nothing_logs_nothing(self):
        app = self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        await self.call("light", "turn_off", "light.majlis", app)  # already off
        self.assertEqual(await self.rows(), [])

    async def test_only_hub_sources_can_tag_a_call(self):
        for source in ("wall_switch", "ha_user", "unknown", "nonsense"):
            with self.assertRaises(ValueError):
                self.recorder.command_context(source, "mem-a")

    async def test_an_automation_chain_started_by_a_person_stays_automation(self):
        # A person toggles a helper in the UI; an automation reacts. Its run
        # context has a parent and no user, so it never counts as the person.
        person = Context(user_id="ha-user-1")
        run = Context(parent_id=person.id)
        self.hass.bus.async_fire("automation_triggered", {}, context=run)
        await self.call("light", "turn_on", "light.majlis", run)
        self.assertEqual(
            await self.rows(),
            [("light.majlis", "on", "automation", None, "majlis")],
        )

    async def test_a_script_a_person_runs_is_the_person(self):
        # Scripts run in their caller's context: a person's, here. The user
        # wins over the script_started marker.
        person = Context(user_id="ha-user-1")
        self.hass.bus.async_fire("script_started", {}, context=person)
        await self.call("light", "turn_on", "light.majlis", person)
        self.assertEqual(
            await self.rows(),
            [("light.majlis", "on", "ha_user", None, "majlis")],
        )

    async def test_a_late_device_report_reads_as_a_wall_press(self):
        # Known limit, as in Home Assistant: a state written more than 5 s
        # after the hub's call has lost the call's context.
        self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        self.press("light.majlis", "on")
        self.assertEqual(
            await self.rows(),
            [("light.majlis", "on", "wall_switch", None, "majlis")],
        )


class FilterTests(RecorderTestCase):
    async def test_flaps_attribute_changes_and_duplicates_are_not_events(self):
        self.press("light.majlis", "off", {"brightness": 10})  # attributes only
        self.press("light.majlis", "unavailable")
        self.press("light.majlis", "on")  # back from unavailable
        self.press("light.majlis", "unknown")
        self.press("light.majlis", "off")
        self.press("light.majlis", "on", {"brightness": 200})
        self.press("light.majlis", "on", {"brightness": 90})  # dimmed
        self.press("cover.majlis_curtain", "opening")
        self.press("cover.majlis_curtain", "open")  # same move
        self.press("climate.majlis_ac", "cool", {"temperature": 22})
        self.press("climate.majlis_ac", "cool", {"temperature": 24})  # setpoint
        self.press("climate.majlis_ac", "dry")
        self.press("media_player.majlis_tv", "playing")
        self.press("media_player.majlis_tv", "paused")
        self.press("media_player.majlis_tv", "standby")
        self.assertEqual(
            await self.rows(),
            [
                ("light.majlis", "on", "wall_switch", None, "majlis"),
                ("cover.majlis_curtain", "open", "wall_switch", None, "majlis"),
                ("climate.majlis_ac", "on", "wall_switch", None, "majlis"),
                ("climate.majlis_ac", "set", "wall_switch", None, "majlis"),
                ("media_player.majlis_tv", "on", "wall_switch", None, "majlis"),
                ("media_player.majlis_tv", "off", "wall_switch", None, "majlis"),
            ],
        )

    async def test_only_the_hubs_devices_in_registry_rooms_are_logged(self):
        for entity_id in (
            "switch.kitchen_right",  # hidden gang
            "switch.kitchen_lock",  # config entity
            "light.kitchen_unimported",  # not imported
            "light.hall",  # not a registry room
            "light.nowhere",  # no room at all
        ):
            self.press(entity_id, "on")
        self.hass.states.async_set("sensor.majlis_power", "12")
        self.hass.states.async_set("sensor.majlis_power", "40")
        self.assertEqual(await self.rows(), [])

    async def test_entities_added_or_removed_are_not_events(self):
        self.hass.states.async_remove("light.majlis_spot")
        self.press("light.majlis_spot", "on")  # comes back: old_state is None
        self.assertEqual(await self.rows(), [])

    async def test_a_chattering_device_is_capped(self):
        with self.assertLogs(recorder_module.__name__, logging.WARNING) as logs:
            for i in range(30):
                self.press("light.majlis", "on" if i % 2 == 0 else "off")
            await self.hass.async_block_till_done()
        self.assertEqual(len(await self.rows()), 20)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("light.majlis changes too often", logs.output[0])
        # Other devices are unaffected.
        self.press("light.majlis_spot", "on")
        self.assertEqual(len(await self.rows()), 21)


class StartupTests(RecorderTestCase):
    already_running = False

    async def test_restart_produces_no_events(self):
        # Home Assistant is starting: integrations restore and reconnect.
        self.hass.set_state(CoreState.starting)
        self.press("light.majlis", "unavailable")
        self.press("light.majlis", "on")
        self.press("light.majlis_spot", "on")  # restored off, then the real on
        self.hass.states.async_set("light.majlis_new", "on")
        self.hass.set_state(CoreState.running)
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done()
        # Still settling.
        self.press("light.majlis_spot", "off")
        self.assertEqual(await self.rows(), [])

        await asyncio.sleep(0.1)
        await self.hass.async_block_till_done()
        self.press("light.majlis_spot", "on")
        self.assertEqual(
            await self.rows(),
            [("light.majlis_spot", "on", "wall_switch", None, "majlis")],
        )

    async def test_a_reload_of_a_running_hub_records_at_once_and_invents_nothing(self):
        await self.recorder.async_stop()
        self.hass.set_state(CoreState.running)
        self.recorder = ActivityRecorder(
            self.hass, self.registry, self.storage.room_activity()
        )
        await self.recorder.async_start()
        self.assertEqual(await self.rows(), [])
        self.press("light.majlis", "on")
        self.assertEqual(len(await self.rows()), 1)


class RegistryTests(RecorderTestCase):
    async def test_a_newly_imported_device_is_logged_after_the_registry_changes(self):
        self.press("light.kitchen_unimported", "on")
        self.storage.table("registry_user_devices")["dev-new"] = {
            "entity_ids": ["light.kitchen_unimported"]
        }
        with mock.patch.object(recorder_module, "_REGISTRY_DEBOUNCE_SECONDS", 0):
            self.hass.bus.async_fire("casasmart_registry_changed", {"kind": "devices"})
            await self.hass.async_block_till_done()
            await asyncio.sleep(0.01)
            await self.hass.async_block_till_done()
        self.press("light.kitchen_unimported", "off")
        self.assertEqual(
            await self.rows(),
            [("light.kitchen_unimported", "off", "wall_switch", None, "kitchen")],
        )

    async def test_rooms_resolve_while_the_entry_is_still_loading(self):
        # During a reload the entry isn't "loaded" yet, so a lookup through
        # the loaded entry finds no registry; the recorder uses its own.
        self.hass.config_entries = types.SimpleNamespace(
            async_loaded_entries=lambda domain: []
        )
        self.press("light.majlis", "on")
        self.assertEqual(
            await self.rows(),
            [("light.majlis", "on", "wall_switch", None, "majlis")],
        )

    async def test_a_moved_device_keeps_its_history_and_a_deleted_room_too(self):
        table = self.storage.room_activity()
        self.press("light.majlis", "on")
        self.registry.assign_device("light.majlis", room_id="kitchen")
        self.press("light.majlis", "off")
        self.assertEqual(
            [(r[1], r[4]) for r in await self.rows()],
            [("on", "majlis"), ("off", "kitchen")],
        )

        self.registry.delete_room("kitchen")
        self.press("light.majlis", "on")  # Unassigned now: no room, no event
        self.assertEqual(len(await self.rows()), 2)
        # The deleted room's rows stay where they were recorded.
        self.assertEqual(
            table.last_off_by_room(["kitchen", "majlis"])["kitchen"]["entity_id"],
            "light.majlis",
        )
        self.assertNotIn("majlis", table.last_off_by_room(["majlis"]))


class WriteTests(RecorderTestCase):
    async def test_rows_carry_utc_time_and_the_hub_zones_offset(self):
        self.press("light.majlis", "on")
        await self.rows()
        (row,) = self.storage.room_activity().events()
        self.assertEqual(row["utc_offset_min"], 180)  # Asia/Riyadh
        state = self.hass.states.get("light.majlis")
        self.assertEqual(row["t"], int(state.last_changed.timestamp()))

    async def test_writes_run_in_the_executor_in_one_transaction(self):
        threads = []
        table = self.storage.room_activity()
        real = table.append_many

        def spy(rows):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(rows)

        with mock.patch.object(table, "append_many", spy):
            self.recorder._table = table
            for entity_id in (
                "light.majlis",
                "light.majlis_spot",
                "cover.majlis_curtain",
            ):
                self.press(entity_id, "open" if entity_id.startswith("cover") else "on")
            await self.hass.async_block_till_done()
            self.assertEqual(table.count(), 0)  # buffered, not written yet
            await self.recorder.async_flush()
        self.assertEqual(threads, [False])  # one batch, off the event loop
        self.assertEqual(table.count(), 3)

    async def test_a_burst_of_100_is_written_without_waiting(self):
        # 100 devices switched at once (a big scene), one event each.
        for i in range(100):
            entity_id = f"light.burst_{i}"
            self.storage.table("registry_devices")[entity_id] = {
                "room_id": "majlis",
                "display_name": None,
                "sort_order": 0,
            }
            self.hass.states.async_set(entity_id, "off")
        self.registry.warm_up()
        self.storage.table("registry_user_devices")["dev-burst"] = {
            "entity_ids": [f"light.burst_{i}" for i in range(100)]
        }
        await self.recorder._async_load_managed()
        for i in range(100):
            self.press(f"light.burst_{i}", "on")
        await self.hass.async_block_till_done()
        for _ in range(20):
            if self.storage.room_activity().count() == 100:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.storage.room_activity().count(), 100)

    async def test_the_flush_timer_writes_a_quiet_buffer(self):
        await self.recorder.async_stop()
        self.recorder = ActivityRecorder(
            self.hass, self.registry, self.storage.room_activity()
        )
        with mock.patch.object(recorder_module, "_FLUSH_SECONDS", 0.02):
            await self.recorder.async_start()
            self.press("light.majlis", "on")
            await self.hass.async_block_till_done()
            for _ in range(50):
                if self.storage.room_activity().count():
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.storage.room_activity().count(), 1)

    async def test_stop_writes_the_buffer_and_stops_listening(self):
        listeners = self.hass.bus.async_listeners()
        self.press("light.majlis", "on")
        await self.hass.async_block_till_done()
        await self.recorder.async_stop()
        self.assertEqual(self.storage.room_activity().count(), 1)
        self.press("light.majlis", "off")
        await self.hass.async_block_till_done()
        await self.recorder.async_flush()
        self.assertEqual(self.storage.room_activity().count(), 1)
        after = self.hass.bus.async_listeners()
        for event_type in (
            "state_changed",
            "call_service",
            "automation_triggered",
            "script_started",
            "casasmart_registry_changed",
        ):
            self.assertEqual(
                after.get(event_type, 0), listeners.get(event_type, 0) - 1, event_type
            )
        await self.recorder.async_stop()  # twice is fine

    async def test_a_failed_write_is_logged_and_dropped(self):
        self.press("light.majlis", "on")
        await self.hass.async_block_till_done()
        self.storage.close()
        with self.assertLogs(recorder_module.__name__, logging.WARNING) as logs:
            await self.recorder.async_flush()
        self.assertIn("1 events not saved", logs.output[0])
        self.storage.open()


class PrivacyTests(RecorderTestCase):
    async def test_forget_member_drops_written_and_buffered_rows(self):
        mine = self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        theirs = self.recorder.command_context(activity.SOURCE_APP, "mem-b")
        await self.call("light", "turn_on", "light.majlis", mine)
        await self.rows()  # written
        await self.call("light", "turn_off", "light.majlis", mine)  # buffered
        await self.call("light", "turn_on", "light.majlis_spot", theirs)
        pending = self.recorder.command_context(activity.SOURCE_SCENE, "mem-a")

        await self.recorder.async_forget_member("mem-a")
        # A change still arriving in the departed member's context is no
        # longer credited to them: it is an unnamed caller's command now.
        await self.call("light", "turn_on", "light.majlis", pending)
        self.assertEqual(
            await self.rows(),
            [
                ("light.majlis_spot", "on", "app", "mem-b", "majlis"),
                ("light.majlis", "on", "unknown", None, "majlis"),
            ],
        )

    async def test_clear_drops_written_and_buffered_rows(self):
        self.press("light.majlis", "on")
        await self.rows()
        self.press("light.majlis", "off")
        await self.hass.async_block_till_done()
        await self.recorder.async_clear()
        self.assertEqual(await self.rows(), [])


class RetentionTests(RecorderTestCase):
    async def test_prune_applies_age_source_and_cap_rules(self):
        now = 1_800_000_000
        day = 86400
        table = self.storage.room_activity()

        def row(age_days, source):
            return {
                "t": now - int(age_days * day),
                "utc_offset_min": 180,
                "room_id": "majlis",
                "entity_id": "light.majlis",
                "action": "on",
                "source": source,
            }

        table.append_many(
            [
                row(30, "automation"),
                row(32, "automation"),
                row(399, "wall_switch"),
                row(401, "wall_switch"),
                row(401, "app"),
                row(380, "ha_user"),
            ]
        )
        self.recorder._wall_clock = lambda: now
        await self.recorder.async_prune()
        self.assertEqual(
            sorted((now - r["t"]) // day for r in table.events()), [30, 380, 399]
        )

    async def test_the_row_cap_drops_the_oldest_with_a_warning(self):
        table = self.storage.room_activity()
        table.append_many(
            [
                {
                    "t": 1_800_000_000 - i,
                    "utc_offset_min": 0,
                    "room_id": "majlis",
                    "entity_id": "light.majlis",
                    "action": "on",
                    "source": "wall_switch",
                }
                for i in range(5)
            ]
        )
        self.recorder._wall_clock = lambda: 1_800_000_000
        with (
            mock.patch.object(activity, "MAX_ROWS", 3),
            self.assertLogs(recorder_module.__name__, logging.WARNING) as logs,
        ):
            await self.recorder.async_prune()
        self.assertEqual(table.count(), 3)
        self.assertEqual(min(r["t"] for r in table.events()), 1_800_000_000 - 2)
        self.assertIn("dropped the oldest 2", logs.output[0])


if __name__ == "__main__":
    unittest.main()
