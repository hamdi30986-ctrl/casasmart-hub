"""A factory reset (a new owner) wipes everything the hub learned about rooms.

A reset is for a new owner and wipes the habits too. The real casasmart.factory_reset handler runs over a temp
HubStorage holding every kind of room-intel data, written by the real
components: the ActivityRecorder (on a real Home Assistant core: written and
still-buffered events, saved and unsaved last-on sets), the HabitLearner
(household and personal models, Ramadan models, a kept Ramadan, settings),
the NowDataEngine (recents, captures, room-command answers, start profiles)
and a migration backup. Afterwards nothing is left: not in the tables, not
in memory, and not on the disk (the old owner's member id is searched for in
the raw database, WAL and backup bytes). The admin "Reset habits" keeps its
scope. Real-HA environment only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.const import DOMAIN, FACTORY_RESET_TABLES
    from casasmart.room_intel.hijri import ramadans_before
    from casasmart.room_intel.learner import HabitLearner
    from casasmart.room_intel.recorder import ActivityRecorder
    from casasmart.storage import StorageError
    from casasmart.storage.migrations import backup_database
    from homeassistant.core import CoreState, HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    _async_register_services = H.import_integration()._async_register_services
    _ERR = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR

# Only the old owner's room data carries these, so finding one anywhere on
# the disk after the reset means something survived.
OLD_MEMBER = "mem-zq7-old-owner"
LIGHT = "light.zq7_majlis"
FAN = "fan.zq7_majlis"
ROOM = "majlis"
OFFSET_MIN = 180  # Riyadh
ROOM_INTEL_TABLES = (
    "now_recents",
    "now_room_policies",
    "now_restore_sets",
    "now_idempotency",
    "now_room_habits",
    "now_habit_config",
    "now_ramadan_habits",
    "now_room_last_on",
    "now_room_start_profiles",
)


class _NoEntityRegistry:
    def async_get(self, entity_id):
        return None


class _Auth:
    async def async_get_user(self, user_id):
        return types.SimpleNamespace(is_admin=True)


def _row(day, hour, action="on", source="app", member=OLD_MEMBER, entity=LIGHT):
    """A log row at a local day and hour (Riyadh)."""
    return {
        "t": day * 86400 + hour * 3600 - OFFSET_MIN * 60,
        "utc_offset_min": OFFSET_MIN,
        "room_id": ROOM,
        "entity_id": entity,
        "action": action,
        "source": source,
        "member_id": member if source in {"app", "scene"} else None,
    }


@unittest.skipIf(_SKIP, f"CasaSmart unimportable: {_SKIP}")
class FactoryResetRoomIntelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The service handler's hass: the harness's (admin check, executor).
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.storage = self.rt.storage
        self.db_path = Path(self._tmp.name) / "view-test.db"
        self.storage.table("registry_rooms")[ROOM] = {
            "name": "Majlis",
            "floor_id": None,
            "icon": None,
            "sort_order": 0,
        }
        for eid in (LIGHT, FAN):
            self.storage.table("registry_devices")[eid] = {
                "room_id": ROOM,
                "display_name": None,
                "sort_order": 0,
            }
        self.storage.table("registry_user_devices")["dev-zq7"] = {
            "entity_ids": [LIGHT, FAN]
        }
        self.rt.registry.warm_up()
        self.hass.auth = _Auth()
        self.hass.config_entries.async_reload = mock.AsyncMock(return_value=True)
        self.rt.energy_controller = None
        self.rt.energy_flags = types.SimpleNamespace(disabled_automations=lambda: [])
        _async_register_services(self.hass)

        # The recorder runs on a real Home Assistant core.
        self.core = HomeAssistant(self._tmp.name)
        self.core.config.time_zone = "Asia/Riyadh"
        self.core.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        for eid in (LIGHT, FAN):
            self.core.states.async_set(eid, "off")
        await self.core.async_block_till_done()
        self.core.set_state(CoreState.running)
        self.recorder = ActivityRecorder(
            self.core,
            self.rt.registry,
            self.storage.room_activity(),
            last_on_table=self.storage.table("now_room_last_on"),
        )
        await self.recorder.async_start()
        self.addAsyncCleanup(self.recorder.async_stop)
        self.rt.room_activity = self.recorder
        self.learner = HabitLearner(
            self.hass,
            self.storage.table("now_room_habits"),
            self.storage.table("now_habit_config"),
            self.storage.room_activity(),
            ramadan_table=self.storage.table("now_ramadan_habits"),
            recorder=self.recorder,
        )
        await self.learner.async_start()
        self.rt.room_habits = self.learner

    async def press(self, eid, state):
        self.core.states.async_set(eid, state)
        await self.core.async_block_till_done()

    async def fill_with_the_old_owners_data(self):
        today = int((time.time() + OFFSET_MIN * 60) // 86400)
        ramadan = next(iter(ramadans_before(today)))
        rows = [
            # Last Ramadan, every evening, and the regular weeks since.
            *(_row(day, 21) for day in range(ramadan.first, ramadan.last + 1)),
            *(_row(day, 20) for day in range(today - 30, today)),
            *(_row(day, 23, "off", "wall_switch") for day in range(today - 30, today)),
            _row(today - 1, 6, "on", "automation"),
        ]
        self.storage.room_activity().append_many(rows)
        # The kept Ramadan (the daily job) and today's regular model, both
        # layers, built by the real learner.
        await self.learner.async_maintain()
        await self.learner.async_update({"weekend_days": [4, 5]}, managed_ids={LIGHT})
        query = self.learner.query(OLD_MEMBER)
        await query.async_learned([ROOM], frozenset({LIGHT, FAN}))
        # A Ramadan model as the learner saves one during Ramadan.
        models = self.storage.table("now_room_habits")
        for key in ("ramadan:household", f"ramadan:member:{OLD_MEMBER}"):
            models[key] = {
                "v": 2,
                "profile": "ramadan",
                "day": ramadan.last,
                "sig": "x",
                "layer": {"rooms": {ROOM: {"member": OLD_MEMBER}}},
            }
        # Now page data.
        now_data = self.rt.now_data
        now_data.set_room_policy(ROOM, True, [LIGHT, FAN])
        now_data.record_successful_control(OLD_MEMBER, LIGHT)
        now_data.save_idempotent_result(
            OLD_MEMBER, ROOM, "turn_off", "key-zq7-0001", {"ok": True}
        )
        now_data.save_restore_set(ROOM, [LIGHT])
        now_data.set_start_profile(ROOM, [LIGHT, FAN])
        # A saved last-on set, then an unsaved one and a buffered event.
        await self.press(LIGHT, "on")
        await self.press(LIGHT, "off")
        await self.recorder.async_flush()
        await self.press(FAN, "on")
        await self.press(FAN, "off")
        # The migration backup a v4 -> v5 update leaves behind.
        self.backup = backup_database(self.db_path, self.db_path.parent / "backups")
        self.unrelated = self.db_path.parent / "backups" / "notes.txt"
        self.unrelated.write_text("not a hub backup")

    def disk_bytes(self):
        data = b""
        for path in (self.db_path, self.db_path.with_name(self.db_path.name + "-wal")):
            if path.exists():
                data += path.read_bytes()
        return data

    async def factory_reset(self):
        handler = self.hass.services.handlers[(DOMAIN, "factory_reset")]
        await handler(
            types.SimpleNamespace(
                data={}, context=types.SimpleNamespace(user_id="admin")
            )
        )

    async def test_every_room_intel_store_is_wiped_in_storage_memory_and_on_disk(self):
        await self.fill_with_the_old_owners_data()
        # Everything is there before.
        for table in ROOM_INTEL_TABLES:
            with self.subTest(before=table):
                self.assertGreater(len(self.storage.table(table)), 0)
        self.assertEqual(
            {key.split(":")[0] for key in self.storage.table("now_room_habits")},
            {"regular", "ramadan"},
        )
        self.assertIn(
            f"regular:member:{OLD_MEMBER}", self.storage.table("now_room_habits")
        )
        self.assertTrue(self.storage.table("now_ramadan_habits"))
        self.assertGreater(self.storage.room_activity().count(), 60)
        self.assertTrue(self.recorder._buffer)  # the fan's events
        self.assertTrue(self.recorder.last_on.unsaved)  # the fan's set
        self.assertIsNotNone(self.learner._model)
        self.assertIn(OLD_MEMBER.encode(), self.disk_bytes())
        self.assertIn(OLD_MEMBER.encode(), self.backup.read_bytes())
        for table in ROOM_INTEL_TABLES:
            self.assertIn(table, FACTORY_RESET_TABLES)

        await self.factory_reset()

        for table in ROOM_INTEL_TABLES:
            with self.subTest(after=table):
                self.assertEqual(len(self.storage.table(table)), 0)
        self.assertEqual(self.storage.room_activity().count(), 0)
        # In memory: nothing left to write back, nothing to rank on.
        self.assertEqual(self.recorder._buffer, [])
        self.assertEqual(len(self.recorder.last_on), 0)
        await self.recorder.async_flush()
        self.assertEqual(self.storage.room_activity().count(), 0)
        self.assertEqual(len(self.storage.table("now_room_last_on")), 0)
        self.assertIsNone(self.learner._model)
        # On the disk: the database's free pages and WAL, and the backups.
        self.assertNotIn(OLD_MEMBER.encode(), self.disk_bytes())
        self.assertNotIn(b"zq7", self.disk_bytes())
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.unrelated.exists())
        self.hass.config_entries.async_reload.assert_awaited_once()
        # Learning starts from nothing: no model, no warm-up days, no kept
        # Ramadan, no regular model to go back to after the next one.
        query = self.learner.query(OLD_MEMBER)
        self.assertIsNone(await query.async_learned([ROOM], frozenset({LIGHT, FAN})))
        self.assertEqual(query.summary["status"], "warming_up")
        settings = await self.learner.async_settings([ROOM], frozenset({LIGHT}))
        self.assertEqual(settings["observed_days"], 0)
        self.assertIsNone(settings["reset_at"])
        # The first build marks last Ramadan done with an empty copy (the
        # log has nothing of it any more): listed, but nothing in it.
        kept = dict(self.storage.table("now_ramadan_habits").items())
        self.assertEqual(settings["ramadan"]["kept_years"], [int(k) for k in kept])
        self.assertTrue(all(doc["rows"] == [] for doc in kept.values()))
        self.assertEqual(
            [key for key in self.storage.table("now_room_habits")],
            ["regular:household"],
        )

    async def test_nothing_from_before_the_reload_is_written_after_the_wipe(self):
        # A change between the wipe and the reload (an
        # automation, a wall switch) is the old owner's; the unload's flush
        # used to write it after the wipe.
        await self.fill_with_the_old_owners_data()
        await self.factory_reset()
        await self.press(LIGHT, "on")
        await self.press(LIGHT, "off")
        await self.recorder.async_stop()  # what the reload's unload does
        self.assertEqual(self.storage.room_activity().count(), 0)
        self.assertEqual(len(self.storage.table("now_room_last_on")), 0)
        self.assertEqual(self.recorder._buffer, [])

    async def test_a_scrub_that_fails_still_resets_and_says_run_it_again(self):
        await self.fill_with_the_old_owners_data()
        with mock.patch.object(
            self.storage, "compact", side_effect=StorageError("WAL in use")
        ):
            with self.assertRaisesRegex(HomeAssistantError, "run it again"):
                await self.factory_reset()
        for table in ROOM_INTEL_TABLES:
            self.assertEqual(len(self.storage.table(table)), 0)
        self.assertEqual(self.storage.room_activity().count(), 0)
        self.hass.config_entries.async_reload.assert_awaited_once()
        # Running it again finishes the job.
        await self.factory_reset()
        self.assertNotIn(OLD_MEMBER.encode(), self.disk_bytes())
        self.assertFalse(self.backup.exists())

    async def test_reset_habits_wipes_normal_and_ramadan_habits_only(self):
        await self.fill_with_the_old_owners_data()
        log_rows = self.storage.room_activity().count()
        await self.learner.async_reset()
        self.assertEqual(len(self.storage.table("now_room_habits")), 0)
        self.assertEqual(len(self.storage.table("now_ramadan_habits")), 0)
        self.assertIsNone(self.learner._model)
        # Not habits: the log (by design), the room's last-on set and its
        # start profile, recents and captures stay.
        self.assertEqual(self.storage.room_activity().count(), log_rows)
        self.assertEqual(self.recorder.last_on.entity_ids(ROOM), [FAN])
        self.assertEqual(self.rt.now_data.start_profile(ROOM), [LIGHT, FAN])
        self.assertEqual(self.rt.now_data.restore_set(ROOM), [LIGHT])
        self.assertTrue(self.rt.now_data.recents_for(OLD_MEMBER))
        self.assertIsNotNone(self.learner.settings.reset_at)


if __name__ == "__main__":
    unittest.main()
