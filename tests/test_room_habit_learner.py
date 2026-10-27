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
            ramadan_table=self.storage.table("now_ramadan_habits"),
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

    def kept(self):
        return dict(self.storage.table("now_ramadan_habits").items())


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
            self.assertEqual(
                summary,
                {"layer": "household", "status": "learning", "profile": "regular"},
            )
            self.assertEqual(reasons, {"majlis": "habit_now"})
            sources = ("app", "scene", "ha_user", "wall_switch")
            # Once: the last Ramadan (1447, 2026-02-18 to 03-19) is copied out
            # of the log to be kept. Then the window: the 56 dates before
            # today, every human source.
            self.assertEqual(
                reads,
                [
                    {"first_day": 20502, "last_day": 20531, "sources": sources},
                    {
                        "first_day": TODAY - 56,
                        "last_day": TODAY - 1,
                        "sources": sources,
                    },
                ],
            )
            # The rest of the day is answered from memory...
            for hour in (21, 22, 23):
                self.now = at(TODAY, hour, 10)
                await self.ask()
            self.assertEqual(len(reads), 2)
            # ...and the next day reads the window again, not the Ramadan.
            self.now = at(TODAY + 1, 7, 10)
            await self.ask()
            self.assertEqual(len(reads), 3)
            self.assertEqual(reads[2]["last_day"], TODAY)
            self.assertEqual(self.learner.builds, 2)
        # The buffered seconds were written before each read.
        self.assertEqual(self.recorder.flushes, 2)

    async def test_the_query_says_which_devices_usually_come_on(self):
        self.household_routine()
        query = self.learner.query(None)
        self.assertEqual(query.usual_devices("majlis"), [])
        await query.async_learned(ROOMS, COUNTED, self.now)
        self.assertEqual(query.usual_devices("majlis"), ["light.majlis"])
        self.assertEqual(query.usual_devices("kitchen"), [])
        # A restart the same day loads the saved per-device votes.
        restarted = await self.make_learner()
        query = restarted.query(None)
        await query.async_learned(ROOMS, COUNTED, self.now)
        self.assertEqual(restarted.loads, 1)
        self.assertEqual(query.usual_devices("majlis"), ["light.majlis"])
        # Learning off: no habits, so no usual devices either.
        await self.learner.async_update({"learning": False}, managed_ids=())
        query = self.learner.query(None)
        await query.async_learned(ROOMS, COUNTED, self.now)
        self.assertEqual(query.usual_devices("majlis"), [])

    async def test_a_saved_version_2_model_is_rebuilt(self):
        self.household_routine()
        await self.ask()
        table = self.storage.table("now_room_habits")
        for key, doc in table.items():
            table[key] = {**doc, "v": 2}
        restarted = await self.make_learner()
        _, reasons = await self.ask(learner=restarted)
        self.assertEqual(reasons, {"majlis": "habit_now"})
        self.assertEqual((restarted.builds, restarted.loads), (1, 0))

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
            self.assertEqual({k: doc[k] for k in ("v", "day")}, {"v": 3, "day": TODAY})
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
        # So is one with the right header and no layer, or a member doc
        # whose layer isn't an object.
        docs = self.docs()
        for key, bad in (
            ("regular:household", {k: v for k, v in docs["regular:household"].items()
                                   if k != "layer"}),
            ("regular:member:mem-a", {**docs["regular:member:mem-a"], "layer": None}),
        ):  # fmt: skip
            with self.subTest(key=key):
                self.storage.table("now_room_habits")[key] = bad
                restarted = await self.make_learner()
                _, reasons = await self.ask(learner=restarted)
                self.assertEqual(reasons, {"majlis": "habit_now"})
                self.assertEqual((restarted.builds, restarted.loads), (1, 0))

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
                "profile": "regular",
                # The next Ramadan, and the last one kept for it (Ramadan
                # 1447 ended 2026-03-19; nothing was logged then).
                "ramadan": {
                    "year": 1448,
                    "first_day": "2027-02-08",
                    "last_day": "2027-03-08",
                    "kept_years": [1447],
                },
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
        self.assertTrue(self.kept())
        await self.learner.async_clear()
        self.assertEqual(self.docs(), {})
        self.assertEqual(self.kept(), {})
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
        self.assertEqual(
            mine, {"layer": "personal", "status": "learning", "profile": "regular"}
        )
        # A's own taps say bedroom; the majlis' wall-press habit is narrowed.
        self.assertEqual(reasons, {"bedroom": "habit_now"})
        # Someone with no app history gets the household's.
        _, reasons = await self.ask("mem-b")
        self.assertEqual(reasons, {"majlis": "habit_now", "bedroom": "habit_now"})

    async def test_wall_presses_are_saved_in_the_household_layer_only(self):
        await self.ask("mem-a")
        docs = self.docs()
        self.assertEqual(sorted(docs), ["regular:household", "regular:member:mem-a"])
        # Full-weight wall votes in the household; A's layer is only A's taps.
        self.assertEqual(
            {day for day, full, half
             in docs["regular:household"]["layer"]["majlis"]["devices"]["light.majlis"]
             if full >> 20 & 1 and not half},
            set(workdays(25)),
        )  # fmt: skip
        self.assertEqual(set(docs["regular:member:mem-a"]["layer"]), {"bedroom"})

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


# Ramadan 1448 by Umm al-Qura: 2027-02-08 (Monday) to 2027-03-08; 1 Shawwal
# (Eid) 2027-03-09, a Tuesday. Ramadan 1447: 2026-02-18 to 2026-03-19.
SHABAN_29 = 20856  # Sunday 2027-02-07
RAMADAN_1 = 20857
RAMADAN_LAST = 20885
EID = 20886
RAMADAN = range(RAMADAN_1, RAMADAN_LAST + 1)
LAST_RAMADAN = range(20502, 20532)


class RamadanTests(LearnerCase):
    """Ramadan on the hub: detected by date, learned apart, the
    regular model back after it, the Ramadan kept for next year."""

    def regular_routine(self, days):
        """The rest of the year: member A switches the majlis on from the
        app at 20:05 on workdays; the kitchen gets a wall press at 07:05."""
        events = []
        for day in days:
            if habits.weekday(day) not in (4, 5):
                events.append(event(day, 20, source="app", member="mem-a"))
            events.append(event(day, 7, room="kitchen", entity="light.kitchen"))
        self.log(events)

    def ramadan_routine(self, days, member="mem-a"):
        """Ramadan: the majlis on at 21:05 (after Taraweeh) from the app, the
        kitchen at 03:05 (suhoor) from the wall, every day."""
        self.log(
            [event(day, 21, source="app", member=member) for day in days]
            + [event(day, 3, room="kitchen", entity="light.kitchen") for day in days]
        )

    async def ask_at(self, day, hour, member=None, learner=None):
        self.now = at(day, hour, 30)
        return await self.ask(member, learner=learner)

    async def test_ramadan_has_its_own_habits_and_eid_brings_the_regular_ones_back(
        self,
    ):
        self.regular_routine(range(SHABAN_29 - 70, SHABAN_29 + 1))
        self.ramadan_routine(RAMADAN)
        eve = {h: await self.ask_at(SHABAN_29, h) for h in (7, 20, 21, 3)}
        self.assertEqual(eve[7], ({"layer": "household", "status": "learning",
                                   "profile": "regular"}, {"kitchen": "habit_now"}))  # fmt: skip
        self.assertEqual(eve[20][1], {"majlis": "habit_now"})
        self.assertEqual(eve[3][1], {})
        # A first Ramadan: it warms up on its own days, like any new start.
        summary, reasons = await self.ask_at(RAMADAN_1 + 5, 21)
        self.assertEqual(
            (summary["profile"], summary["status"], reasons),
            ("ramadan", "warming_up", {}),
        )
        for hour, expected in ((21, {"majlis": "habit_now"}),
                               (3, {"kitchen": "habit_now"}), (7, {}), (20, {})):  # fmt: skip
            with self.subTest(hour=hour):
                summary, reasons = await self.ask_at(RAMADAN_1 + 20, hour)
                self.assertEqual(summary["status"], "learning")
                self.assertEqual(reasons, expected)
        self.assertEqual(
            sorted(self.docs()),
            ["ramadan:household", "ramadan:member:mem-a"],
        )
        model = self.learner._model
        self.assertEqual(set(model.household["majlis"].seen), set(RAMADAN[:20]))
        # 1 Shawwal: the regular model as it was on the eve, Ramadan unseen.
        for hour in (7, 20, 21, 3):
            with self.subTest(hour=hour):
                self.assertEqual(await self.ask_at(EID, hour), eve[hour])
        model = self.learner._model
        self.assertEqual(model.ages[SHABAN_29], 1)
        self.assertFalse(set(model.household["kitchen"].seen) & set(RAMADAN))
        self.assertEqual(
            sorted(self.docs()), ["regular:household", "regular:member:mem-a"]
        )
        # A phone: member A's own Ramadan taps, blended (personal layer).
        summary, reasons = await self.ask_at(RAMADAN_1 + 20, 21, member="mem-a")
        self.assertEqual(
            (summary["layer"], summary["profile"], reasons),
            ("personal", "ramadan", {"majlis": "habit_now"}),
        )

    async def test_a_finished_ramadan_is_kept_and_answers_next_year(self):
        self.ramadan_routine(LAST_RAMADAN)
        await self.ask()  # 2026-10-22: Ramadan 1447 ended in March
        kept = self.kept()
        self.assertEqual(list(kept), ["1447"])
        copy = kept["1447"]
        self.assertEqual(
            {k: copy[k] for k in ("v", "year", "first_day", "last_day")},
            {"v": 1, "year": 1447, "first_day": 20502, "last_day": 20531},
        )
        self.assertEqual(len(copy["rows"]), 60)  # two devices a day, 30 days
        self.assertEqual(
            copy["rows"][0][:5], ["kitchen", "light.kitchen", "wall_switch", None, 20502]
        )  # fmt: skip
        # The log loses them (its row cap, or its 400 days)...
        self.table.prune(before_t=at(LAST_RAMADAN[-1] + 1, 0))
        self.assertEqual(
            self.table.habit_rows(first_day=0, last_day=TODAY, sources=["app"]), []
        )
        # ...and 1 Ramadan next year still knows Ramadan, from the first day.
        for learner in (self.learner, await self.make_learner()):
            summary, reasons = await self.ask_at(RAMADAN_1, 21, learner=learner)
            self.assertEqual(
                (summary["profile"], summary["status"], reasons),
                ("ramadan", "learning", {"majlis": "habit_now"}),
            )
            _, reasons = await self.ask_at(RAMADAN_1, 3, learner=learner)
            self.assertEqual(reasons, {"kitchen": "habit_now"})
        body = await self.learner.async_settings(ROOMS, COUNTED, self.now)
        self.assertEqual(
            (body["profile"], body["observed_days"], body["ramadan"]),
            ("ramadan", 30, {"year": 1448, "first_day": "2027-02-08",
                              "last_day": "2027-03-08", "kept_years": [1447]}),
        )  # fmt: skip
        # Copied once: a second year's days don't rewrite it.
        self.assertEqual(self.kept()["1447"], copy)

    async def test_a_restart_in_ramadan_reuses_the_saved_model(self):
        self.ramadan_routine(RAMADAN)
        first = await self.ask_at(RAMADAN_1 + 20, 21)
        self.assertEqual(first[1], {"majlis": "habit_now"})
        restarted = await self.make_learner()
        self.assertEqual(
            await self.ask_at(RAMADAN_1 + 20, 21, learner=restarted), first
        )
        self.assertEqual((restarted.builds, restarted.loads), (0, 1))

    async def test_the_hub_off_across_the_end_of_ramadan(self):
        self.regular_routine(range(SHABAN_29 - 70, SHABAN_29 + 1))
        eve = await self.ask_at(SHABAN_29, 20)
        # Last seen on day 27; back two days after Eid.
        self.ramadan_routine(RAMADAN[:27])
        await self.ask_at(RAMADAN_1 + 26, 21)
        self.assertEqual(list(self.kept()), ["1447"])
        restarted = await self.make_learner()
        self.assertEqual(await self.ask_at(EID + 2, 20, learner=restarted), eve)
        copy = self.kept()["1448"]
        self.assertEqual(
            {row[4] for row in copy["rows"]}, set(RAMADAN[:27])
        )  # fmt: skip

    async def test_first_installed_during_ramadan(self):
        # The log starts on day 10 of Ramadan 1448: nothing before.
        self.ramadan_routine(RAMADAN[9:])
        self.regular_routine(range(EID, EID + 20))
        statuses = {}
        for n in (15, 22, 23):
            summary, _ = await self.ask_at(RAMADAN_1 + n, 21)
            statuses[n + 1] = summary["status"]
        # Days 10 to 23 are 14 Ramadan days: habits from the 24th.
        self.assertEqual(statuses, {16: "warming_up", 23: "warming_up", 24: "learning"})
        # After it, the regular year starts from nothing: Ramadan didn't
        # count towards it.
        summary, reasons = await self.ask_at(EID + 10, 20)
        self.assertEqual((summary["status"], reasons), ("warming_up", {}))
        self.assertEqual(
            min(row[4] for row in self.kept()["1448"]["rows"]), RAMADAN[9]
        )  # fmt: skip

    async def test_pause_learning_off_and_reset(self):
        self.ramadan_routine(RAMADAN)
        # Paused from day 5 to day 7 of Ramadan.
        await self.learner.async_update(
            {"paused": True}, managed_ids=(), now_t=at(RAMADAN[4], 10)
        )
        await self.learner.async_update(
            {"paused": False}, managed_ids=(), now_t=at(RAMADAN[6], 10)
        )
        await self.ask_at(EID, 20)
        days = {row[4] for row in self.kept()["1448"]["rows"]}
        self.assertEqual(set(RAMADAN) - days, set(RAMADAN[4:7]))
        # Learning off keeps the kept Ramadans, as it keeps the log.
        await self.learner.async_update(
            {"learning": False}, managed_ids=(), now_t=self.now
        )
        self.assertEqual(self.docs(), {})
        self.assertIn("1448", self.kept())
        # A reset forgets them with everything else.
        await self.learner.async_reset(now_t=self.now)
        self.assertEqual(self.kept(), {})

    async def test_a_departing_member_leaves_nothing_in_the_kept_ramadans(self):
        self.ramadan_routine(LAST_RAMADAN, member="mem-a")
        self.log([event(day, 22, room="bedroom", entity="light.bedroom",
                        source="scene", member="mem-a") for day in LAST_RAMADAN[:5]]
                 + [event(day, 22, room="bedroom", entity="light.bedroom",
                          source="app", member="mem-b") for day in LAST_RAMADAN])  # fmt: skip
        await self.ask()
        rows = self.kept()["1447"]["rows"]
        self.assertEqual({row[3] for row in rows}, {None, "mem-a", "mem-b"})
        # Leave path: the recorder deletes the rows, then the learner forgets.
        self.table.delete_member("mem-a")
        await self.learner.async_forget_member("mem-a")
        rows = self.kept()["1447"]["rows"]
        self.assertEqual({row[3] for row in rows}, {None, "mem-b"})
        self.assertEqual(len(rows), 60)  # the wall presses and B's taps stay
        _, reasons = await self.ask_at(RAMADAN_1, 21)
        self.assertNotIn("majlis", reasons)  # the majlis was A's
        _, reasons = await self.ask_at(RAMADAN_1, 22)
        self.assertEqual(reasons, {"bedroom": "habit_now"})
        self.assertNotIn("ramadan:member:mem-a", self.docs())

    async def test_copies_no_window_reaches_are_deleted(self):
        table = self.storage.table("now_ramadan_habits")
        for year, first, last in ((1445, 19793, 19822), (1446, 20148, 20176)):
            table[str(year)] = {"v": 1, "year": year, "first_day": first,
                                "last_day": last, "rows": []}  # fmt: skip
        await self.ask()
        # Ramadan 1448's window reaches back to 1447 only (1446 is older than
        # the log's 400 days): 1447 is copied from the log, older ones go.
        self.assertEqual(sorted(self.kept()), ["1447"])
        # Once 1448 is over, 1449's window reaches 1448 only: 1447 goes too,
        # so no one's rows are kept longer than the log keeps them.
        await self.ask_at(EID + 1, 20)
        self.assertEqual(sorted(self.kept()), ["1448"])
        self.storage.table("now_ramadan_habits").clear()
        await self.ask_at(EID + 2, 20)
        self.assertEqual(sorted(self.kept()), ["1448"])  # copied again
        # A clock gone back years (a board without a clock, before NTP)
        # deletes nothing newer; a key that isn't a year goes.
        self.storage.table("now_ramadan_habits")["junk"] = {}
        await self.ask_at(17000, 20)  # 2016
        self.assertEqual(sorted(self.kept()), ["1448"])

    async def test_ramadan_is_kept_daily_even_while_learning_is_off(self):
        self.ramadan_routine(RAMADAN)
        self.now = at(RAMADAN_1 + 26, 21)
        await self.learner.async_update(
            {"learning": False}, managed_ids=(), now_t=self.now
        )
        self.now = at(EID, 9)
        await self.learner.async_maintain()
        # Learned while learning was on: days 1 to 26; the rest never.
        self.assertEqual(
            {row[4] for row in self.kept()["1448"]["rows"]}, set(RAMADAN[:26])
        )
        table = self.storage.table("now_ramadan_habits")
        written = table.updated_at("1448")
        await self.learner.async_maintain()  # nothing new: no rewrite
        self.assertEqual(table.updated_at("1448"), written)
        # A storage failure is a warning, not an exception.
        with (
            mock.patch.object(
                self.table, "habit_rows", side_effect=sqlite3.OperationalError("x")
            ),
            self.assertLogs(learner_module.__name__, "WARNING"),
        ):
            self.storage.table("now_ramadan_habits").clear()
            await self.learner.async_maintain()

    async def test_a_copy_outlives_a_correction_of_the_calendar(self):
        self.ramadan_routine(LAST_RAMADAN)
        await self.ask()
        table = self.storage.table("now_ramadan_habits")
        copy = table["1447"]
        # Written when the table said 1447 began a day later: same year, a
        # row a day outside today's dates (dropped), the rest still used.
        table["1447"] = {
            **copy,
            "first_day": LAST_RAMADAN[0] + 1,
            "rows": copy["rows"] + [["majlis", "light.majlis", "app", "mem-a",
                                     LAST_RAMADAN[0] - 1, 1 << 21]],
        }  # fmt: skip
        summary, reasons = await self.ask_at(RAMADAN_1, 21)
        self.assertEqual(
            (summary["status"], reasons), ("learning", {"majlis": "habit_now"})
        )
        self.assertEqual(
            self.learner._model.household["majlis"].seen, set(LAST_RAMADAN)
        )

    async def test_an_unreadable_copy_is_ignored(self):
        self.ramadan_routine(LAST_RAMADAN)
        await self.ask()
        table = self.storage.table("now_ramadan_habits")
        table["1447"] = {**table["1447"], "rows": [["majlis", "light.majlis"]]}
        with self.assertLogs(learner_module.__name__, "DEBUG") as logs:
            summary, reasons = await self.ask_at(RAMADAN_1, 21)
        self.assertEqual((summary["status"], reasons), ("warming_up", {}))
        self.assertIn("kept Ramadan 1447 unreadable", "\n".join(logs.output))
        # A member leaving drops such a copy rather than keep it.
        await self.learner.async_forget_member("mem-a")
        self.assertEqual(self.kept()["1447"]["rows"], [])
