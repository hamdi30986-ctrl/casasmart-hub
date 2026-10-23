"""room_intel.learner over a real HubStorage and room activity table (no HA).

The learner's hass is a stand-in (an executor that runs inline and a time
zone); everything it reads and writes is the real SQLite storage. The whole
path over a real Home Assistant is in test_room_habits_ha.
"""

import asyncio
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"
if "habit_fixture" not in sys.modules:
    _package = ModuleType("habit_fixture")
    _package.__path__ = [str(_ROOT)]
    sys.modules["habit_fixture"] = _package
habits = importlib.import_module("habit_fixture.room_intel.habits")
learner_module = importlib.import_module("habit_fixture.room_intel.learner")
storage_module = importlib.import_module("habit_fixture.storage")
HabitLearner = learner_module.HabitLearner

RIYADH = 180
TODAY = 20748  # 2026-10-22, a Thursday
COUNTED = frozenset({"light.majlis", "light.kitchen", "light.bedroom"})
ROOMS = ["majlis", "kitchen", "bedroom"]


def at(day, hour, minute=0):
    """Epoch seconds of a local Riyadh time."""
    return day * 86400 + hour * 3600 + minute * 60 - RIYADH * 60


def event(
    day,
    hour,
    *,
    room="majlis",
    entity="light.majlis",
    action="on",
    source="wall_switch",
    member=None,
    minute=5,
):
    return {
        "t": at(day, hour, minute),
        "utc_offset_min": RIYADH,
        "room_id": room,
        "entity_id": entity,
        "action": action,
        "source": source,
        "member_id": member,
    }  # fmt: skip


def workdays(count, before=TODAY):
    days, day = [], before - 1
    while len(days) < count:
        if habits.weekday(day) not in (4, 5):
            days.append(day)
        day -= 1
    return days


class FakeHass:
    def __init__(self, time_zone="Asia/Riyadh"):
        self.config = NS(time_zone=time_zone)

    async def async_add_executor_job(self, target, *args):
        # Yields like a real executor hop, so concurrent requests interleave.
        await asyncio.sleep(0)
        return target(*args)


class Recorder:
    def __init__(self):
        self.flushes = 0

    async def async_flush(self):
        self.flushes += 1


class LearnerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = storage_module.HubStorage(Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.table = self.storage.room_activity()
        self.hass = FakeHass()
        self.recorder = Recorder()
        self.now = at(TODAY, 20, 30)  # Thursday 20:30
        self.learner = await self.make_learner()

    async def make_learner(self):
        learner = HabitLearner(
            self.hass,
            self.storage.table("now_room_habits"),
            self.storage.table("now_habit_config"),
            self.table,
            recorder=self.recorder,
            wall_clock=lambda: self.now,
        )
        await learner.async_start()
        return learner

    def log(self, events):
        self.table.append_many(events)

    def household_routine(self, days=None, source="app", member="mem-a"):
        """Evenings: the majlis on at 20:05; mornings: the kitchen at 07:05."""
        days = days or workdays(25)
        events = []
        for day in days:
            events.append(event(day, 20, source=source, member=member))
            events.append(event(day, 7, room="kitchen", entity="light.kitchen"))
        self.log(events)
        return days

    async def ask(self, member=None, rooms=ROOMS, counted=COUNTED, learner=None):
        query = (learner or self.learner).query(member)
        learned = await query.async_learned(rooms, counted, self.now)
        reasons = {}
        if learned is not None:
            for room_id in rooms:
                tier = learned({"room_id": room_id})
                if tier is not None:
                    reasons[room_id] = tier[0]
        return query.summary, reasons

    def docs(self):
        return dict(self.storage.table("now_room_habits").items())


class BuildAndCacheTests(LearnerCase):
    async def test_the_log_is_read_once_a_day(self):
        self.household_routine()
        reads = []
        original = self.table.habit_rows

        def counting(**kwargs):
            reads.append(kwargs)
            return original(**kwargs)

        with mock.patch.object(self.table, "habit_rows", counting):
            summary, reasons = await self.ask()
            self.assertEqual(summary, {"layer": "household", "status": "learning"})
            self.assertEqual(reasons, {"majlis": "habit_now"})
            # The window: the 56 dates before today, every human source.
            self.assertEqual(
                reads,
                [
                    {
                        "first_day": TODAY - 56,
                        "last_day": TODAY - 1,
                        "sources": ("app", "scene", "ha_user", "wall_switch"),
                    }
                ],
            )
            # The rest of the day is answered from memory...
            for hour in (21, 22, 23):
                self.now = at(TODAY, hour, 10)
                await self.ask()
            self.assertEqual(len(reads), 1)
            # ...and the next day reads the log again.
            self.now = at(TODAY + 1, 7, 10)
            await self.ask()
            self.assertEqual(len(reads), 2)
            self.assertEqual(reads[1]["last_day"], TODAY)
            self.assertEqual(self.learner.builds, 2)
        # The buffered seconds were written before each read.
        self.assertEqual(self.recorder.flushes, 2)

    async def test_a_restart_the_same_day_reuses_the_saved_model(self):
        self.household_routine()
        await self.ask()
        self.assertEqual((self.learner.builds, self.learner.loads), (1, 0))
        restarted = await self.make_learner()
        _, reasons = await self.ask(learner=restarted)
        self.assertEqual(reasons, {"majlis": "habit_now"})
        self.assertEqual((restarted.builds, restarted.loads), (0, 1))
        # The next day it builds again.
        self.now = at(TODAY + 1, 20, 30)
        restarted = await self.make_learner()
        await self.ask(learner=restarted)
        self.assertEqual((restarted.builds, restarted.loads), (1, 0))

    async def test_what_rebuilds_and_what_doesnt(self):
        self.household_routine()
        await self.ask()
        self.assertEqual(self.learner.builds, 1)
        # Weekend days only change how dates are grouped: no rebuild.
        await self.learner.async_update(
            {"weekend_days": [5, 6]}, managed_ids=COUNTED, now_t=self.now
        )
        await self.ask()
        self.assertEqual(self.learner.builds, 1)
        # A device that now counts (or stops counting) rebuilds.
        await self.ask(counted=COUNTED | {"light.extra"})
        self.assertEqual(self.learner.builds, 2)
        # So does a device marked as running on its own schedule.
        await self.learner.async_update(
            {"self_scheduled_entity_ids": ["light.kitchen"]},
            managed_ids=COUNTED,
            now_t=self.now,
        )
        await self.ask(counted=COUNTED | {"light.extra"})
        self.assertEqual(self.learner.builds, 3)

    async def test_the_saved_model_is_one_doc_per_layer(self):
        self.household_routine(member="mem-a")
        self.log([event(workdays(1)[0], 9, source="app", member="mem-b")])
        self.log([event(workdays(1)[0], 9, source="scene", member="mem-c")])
        await self.ask()
        docs = self.docs()
        self.assertEqual(
            sorted(docs),
            ["regular:household", "regular:member:mem-a", "regular:member:mem-b"],
        )
        for doc in docs.values():
            self.assertEqual({k: doc[k] for k in ("v", "day")}, {"v": 1, "day": TODAY})
        self.assertEqual(set(docs["regular:household"]["layer"]), {"majlis", "kitchen"})
        # A saved doc that doesn't parse is rebuilt from the log.
        self.storage.table("now_room_habits")["regular:household"] = {
            **docs["regular:household"],
            "layer": {"majlis": "bad"},
        }
        restarted = await self.make_learner()
        _, reasons = await self.ask(learner=restarted)
        self.assertEqual(reasons, {"majlis": "habit_now"})
        self.assertEqual(restarted.builds, 1)

    async def test_a_storage_failure_means_no_habits_warned_once(self):
        self.household_routine()
        with (
            mock.patch.object(
                self.table, "habit_rows", side_effect=sqlite3.OperationalError("locked")
            ),
            self.assertLogs(learner_module.__name__, "DEBUG") as logs,
        ):
            for _ in range(3):
                summary, reasons = await self.ask()
                self.assertEqual(summary["status"], "unavailable")
                self.assertEqual(reasons, {})
        self.assertEqual(
            [r.levelname for r in logs.records], ["WARNING", "DEBUG", "DEBUG"]
        )
        _, reasons = await self.ask()
        self.assertEqual(reasons, {"majlis": "habit_now"})

    async def test_concurrent_requests_build_once(self):
        self.household_routine()
        results = await asyncio.gather(*(self.ask() for _ in range(5)))
        self.assertEqual({json.dumps(r) for r in results}, {json.dumps(results[0])})
        # One build, and no one re-reads the saved model while it is built.
        self.assertEqual((self.learner.builds, self.learner.loads), (1, 0))


class StatusTests(LearnerCase):
    async def test_warming_up_until_two_weeks_of_observed_days(self):
        self.household_routine(workdays(13))
        summary, reasons = await self.ask()
        self.assertEqual((summary["status"], reasons), ("warming_up", {}))
        body = await self.learner.async_settings(ROOMS, COUNTED, self.now)
        self.assertEqual(
            body,
            {
                "learning": True,
                "paused": False,
                "status": "warming_up",
                "weekend_days": [4, 5],
                "self_scheduled_entity_ids": [],
                "reset_at": None,
                "observed_days": 13,
                "warm_up_days": 14,
            },
        )
        self.household_routine(workdays(1, before=workdays(13)[-1]))
        self.now = at(TODAY + 3, 20, 30)  # Sunday: a new day, a new model
        summary, reasons = await self.ask()
        self.assertEqual(
            (summary["status"], reasons), ("learning", {"majlis": "habit_now"})
        )

    async def test_learning_off_shows_no_habits_and_keeps_no_model(self):
        self.household_routine()
        await self.ask()
        self.assertTrue(self.docs())
        await self.learner.async_update(
            {"learning": False}, managed_ids=(), now_t=self.now
        )
        self.assertEqual(self.docs(), {})
        summary, reasons = await self.ask()
        self.assertEqual((summary["status"], reasons), ("off", {}))
        body = await self.learner.async_settings(ROOMS, COUNTED, self.now)
        self.assertEqual((body["status"], body["observed_days"]), ("off", None))
        self.assertEqual(self.docs(), {})
        # Saved: a restart keeps it off.
        restarted = await self.make_learner()
        self.assertFalse(restarted.settings.learning)

    async def test_dates_while_off_are_not_learned_when_it_comes_back(self):
        days = self.household_routine(workdays(30))
        await self.learner.async_update(
            {"learning": False}, managed_ids=(), now_t=at(days[9], 6)
        )
        await self.learner.async_update(
            {"learning": True}, managed_ids=(), now_t=at(TODAY - 1, 23)
        )
        await self.ask()
        layer = self.learner._model.household
        # From the day it went off to the day it came back on, nothing.
        self.assertEqual(max(layer["majlis"].full), days[10])
        self.assertEqual(min(layer["majlis"].full), days[-1])
        self.assertEqual(max(layer["kitchen"].seen), days[10])

    async def test_paused_keeps_ranking_on_what_was_learned(self):
        days = self.household_routine(workdays(30))
        await self.learner.async_update(
            {"paused": True}, managed_ids=(), now_t=at(days[4], 6)
        )
        summary, reasons = await self.ask()
        self.assertEqual(
            (summary["status"], reasons), ("paused", {"majlis": "habit_now"})
        )
        # Nothing from the pause on is learned.
        self.assertLess(max(self.learner._model.household["kitchen"].seen), days[4])
        body = await self.learner.async_settings(ROOMS, COUNTED, self.now)
        self.assertEqual((body["status"], body["paused"]), ("paused", True))

    async def test_reset_forgets_and_learning_starts_again(self):
        self.household_routine()
        await self.ask()
        self.assertTrue(self.docs())
        await self.learner.async_reset(now_t=at(TODAY - 1, 9))
        # The saved model goes at once, not at the next request.
        self.assertEqual(self.docs(), {})
        self.assertEqual(self.learner.settings.reset_at, at(TODAY - 1, 9))
        summary, reasons = await self.ask()
        self.assertEqual((summary["status"], reasons), ("warming_up", {}))
        self.assertEqual(self.learner._model.household, {})
        body = await self.learner.async_settings(ROOMS, COUNTED, self.now)
        self.assertEqual(body["reset_at"], "2026-10-21T06:00:00+00:00")
        self.assertEqual(body["observed_days"], 0)
        # Two days later, only those two days are there.
        self.log([event(TODAY, 20), event(TODAY + 1, 20)])
        self.now = at(TODAY + 2, 20, 30)
        await self.ask()
        self.assertEqual(
            sorted(self.learner._model.household["majlis"].seen), [TODAY, TODAY + 1]
        )

    async def test_factory_reset_clears_the_model_and_the_settings(self):
        self.household_routine()
        await self.learner.async_update(
            {"paused": True}, managed_ids=(), now_t=self.now
        )
        await self.ask()
        await self.learner.async_clear()
        self.assertEqual(self.docs(), {})
        self.assertEqual(dict(self.storage.table("now_habit_config").items()), {})
        self.assertEqual(self.learner.settings, habits.HabitSettings())

    async def test_an_invalid_change_changes_nothing(self):
        before = dict(self.storage.table("now_habit_config").items())
        with self.assertRaises(habits.HabitSettingsError):
            await self.learner.async_update(
                {"self_scheduled_entity_ids": ["light.nope"]}, managed_ids=COUNTED
            )
        self.assertEqual(dict(self.storage.table("now_habit_config").items()), before)
        self.assertEqual(self.learner.settings, habits.HabitSettings())


class LayerAndMemberTests(LearnerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        days = workdays(25)
        # Wall presses light the majlis every evening; member A switches the
        # bedroom on from the app at 20:00 most evenings.
        self.household_routine(days, source="wall_switch", member=None)
        self.log(
            [event(day, 20, room="bedroom", entity="light.bedroom", source="app",
                   member="mem-a") for day in days[:20]]
        )  # fmt: skip

    async def test_the_tablet_gets_the_household_a_phone_its_member(self):
        household, reasons = await self.ask(None)
        self.assertEqual(household["layer"], "household")
        self.assertEqual(reasons, {"majlis": "habit_now", "bedroom": "habit_now"})
        mine, reasons = await self.ask("mem-a")
        self.assertEqual(mine, {"layer": "personal", "status": "learning"})
        # A's own taps say bedroom; the majlis' wall-press habit is narrowed.
        self.assertEqual(reasons, {"bedroom": "habit_now"})
        # Someone with no app history gets the household's.
        _, reasons = await self.ask("mem-b")
        self.assertEqual(reasons, {"majlis": "habit_now", "bedroom": "habit_now"})

    async def test_a_departing_member_is_forgotten(self):
        await self.ask("mem-a")
        self.assertIn("regular:member:mem-a", self.docs())
        # Leave path: the recorder deletes the rows, then the learner forgets.
        self.table.delete_member("mem-a")
        await self.learner.async_forget_member("mem-a")
        self.assertEqual(self.docs(), {})
        _, reasons = await self.ask(None)
        self.assertEqual(reasons, {"majlis": "habit_now"})
        self.assertEqual(self.learner.builds, 2)
        self.assertNotIn("mem-a", json.dumps(self.docs()))

    async def test_a_scoped_member_is_answered_from_their_rooms_only(self):
        baseline = await self.ask("mem-b", rooms=["majlis"])
        # Lots more activity in rooms the member can't see.
        self.log(
            [event(day, hour, room="bedroom", entity="light.bedroom")
             for day in workdays(50) for hour in (3, 20)]
        )  # fmt: skip
        self.learner._drop_model()  # rebuilt with it
        self.assertEqual(await self.ask("mem-b", rooms=["majlis"]), baseline)


class LocalTimeTests(LearnerCase):
    async def test_riyadh_days_turn_at_local_midnight(self):
        before = self.learner._local_now(at(TODAY, 23, 59) + 59)
        after = self.learner._local_now(at(TODAY + 1, 0))
        self.assertEqual(before, (TODAY, 23, RIYADH))
        self.assertEqual(after, (TODAY + 1, 0, RIYADH))
        # A hub set to UTC is three hours behind.
        self.hass.config.time_zone = "UTC"
        self.assertEqual(self.learner._local_now(at(TODAY + 1, 0)), (TODAY, 21, 0))
        self.hass.config.time_zone = "Not/AZone"
        self.assertEqual(self.learner._local_now(at(TODAY + 1, 0)), (TODAY, 21, 0))


if __name__ == "__main__":
    unittest.main()
