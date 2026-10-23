"""room_intel.habits: the room habit model's math, layers and settings (no HA).

The rules are pinned here on hand-built activity rows; the rows the log
really returns are pinned in test_room_activity_table (habit_rows) and the
whole path over a real Home Assistant in test_room_habits_ha.
"""

import importlib
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"
if "habit_fixture" not in sys.modules:
    _package = ModuleType("habit_fixture")
    _package.__path__ = [str(_ROOT)]
    sys.modules["habit_fixture"] = _package
habits = importlib.import_module("habit_fixture.room_intel.habits")

RIYADH = 180
# 2026-10-22, a Thursday (a workday in Saudi Arabia); day numbers count from
# 1970-01-01.
TODAY = 20748
assert habits.weekday(TODAY) == 3
COUNTED = frozenset({"light.majlis", "light.kitchen", "climate.majlis"})
H = 20  # 20:00 local


def row(
    day,
    hours,
    *,
    room="majlis",
    entity="light.majlis",
    source="wall_switch",
    member=None,
):
    mask = 0
    for hour in hours:
        mask |= 1 << hour
    return {
        "room_id": room,
        "entity_id": entity,
        "source": source,
        "member_id": member,
        "local_day": day,
        "on_hours": mask,
    }  # fmt: skip


def workdays(count, *, before=TODAY):
    """The last `count` workdays (Fri and Sat are the weekend) before a day."""
    days = []
    day = before - 1
    while len(days) < count:
        if habits.weekday(day) not in (4, 5):
            days.append(day)
        day -= 1
    return days


def build(rows, *, counted=COUNTED, scheduled=(), usable=None):
    return habits.build_model(
        rows,
        counted=counted,
        self_scheduled=frozenset(scheduled),
        usable=usable or habits.HabitSettings().usable_days(TODAY, RIYADH),
    )


def scorer(
    model,
    rooms=("majlis", "kitchen"),
    *,
    member=None,
    hour=H,
    today=TODAY,
    weekend=(4, 5),
):
    return habits.HabitScorer(
        model, rooms, today=today, hour=hour, weekend_days=weekend, member_id=member
    )  # fmt: skip


def presence(days, room="kitchen"):
    """Someone was home on these days: a wall press elsewhere, at 07:00."""
    return [row(day, [7], room=room, entity="light.kitchen") for day in days]


class DecayTests(unittest.TestCase):
    def test_a_vote_halves_every_fourteen_days(self):
        evidence = habits.RoomEvidence(full={TODAY - 14: 1 << H})
        habit, dates = habits.strength(evidence, [TODAY - 14], hour=H, today=TODAY)
        self.assertAlmostEqual(habit, 0.5 / (0.5 + 4))
        self.assertEqual(dates, 1)
        evidence = habits.RoomEvidence(full={TODAY - 28: 1 << H})
        habit, _ = habits.strength(evidence, [TODAY - 28], hour=H, today=TODAY)
        self.assertAlmostEqual(habit, 0.25 / (0.25 + 4))

    def test_an_old_habit_fades_and_a_new_one_takes_over(self):
        days = workdays(39)  # the window's workdays, newest first
        old, recent = days[20:], days[:20]
        rows = presence(days)
        rows += [row(day, [H], source="app", member="mem-a") for day in old]
        rows += [
            row(day, [H], room="kitchen", entity="light.kitchen", source="app",
                member="mem-a")
            for day in recent
        ]  # fmt: skip
        model = build(rows)
        majlis, _ = scorer(model).score("majlis")
        kitchen, _ = scorer(model).score("kitchen")
        # Same number of evenings each; the recent ones weigh far more.
        self.assertLess(majlis, habits.MIN_STRENGTH)
        self.assertGreater(kitchen, habits.MIN_STRENGTH)
        self.assertGreater(kitchen, 3 * majlis)

    def test_only_the_window_counts_and_today_is_left_out(self):
        usable = habits.HabitSettings().usable_days(TODAY, RIYADH)
        self.assertEqual((usable.first, usable.last), (TODAY - 56, TODAY - 1))
        self.assertFalse(usable(TODAY))
        self.assertFalse(usable(TODAY - 57))
        model = build([row(TODAY, [H]), row(TODAY - 57, [H]), row(TODAY - 1, [H])])
        self.assertEqual(set(model.household["majlis"].full), {TODAY - 1})


class GateTests(unittest.TestCase):
    def model(self, evenings, observed=20, source="app"):
        days = workdays(observed)
        return build(
            presence(days)
            + [row(day, [H], source=source, member="mem-a") for day in days[:evenings]]
        )

    def test_strength_threshold(self):
        # 20 observed workdays, the room switched on at 20:00 on the newest n.
        # A wall press weighs as much as an app tap (at
        # half weight it took 19 of the 20 evenings).
        for source, first in (("app", 7), ("wall_switch", 7)):
            previous = 0.0
            for evenings in range(1, 21):
                with self.subTest(source=source, evenings=evenings):
                    s = scorer(self.model(evenings, source=source))
                    habit, dates = s.score("majlis")
                    learned = s.learned({"room_id": "majlis"})
                    self.assertGreater(habit, previous)
                    self.assertEqual(dates, evenings)
                    self.assertEqual(habit >= habits.MIN_STRENGTH, evenings >= first)
                    if evenings < first:
                        self.assertIsNone(learned)
                    else:
                        # Stronger habits sort first, in the habit sub-slot.
                        self.assertEqual(
                            learned, ("habit_now", {}, (0, -round(habit, 4)))
                        )
                    previous = habit

    def test_three_distinct_days_of_evidence(self):
        # The prior keeps two days under 0.35 anyway; with a lower bar the
        # evidence rule still holds on its own.
        with mock.patch.object(habits, "MIN_STRENGTH", 0.05):
            two = scorer(self.model(2))
            three = scorer(self.model(3))
            self.assertEqual(two.score("majlis")[1], 2)
            self.assertIsNone(two.learned({"room_id": "majlis"}))
            self.assertIsNotNone(three.learned({"room_id": "majlis"}))

    def test_nothing_ranks_before_two_weeks_of_observed_days(self):
        days = workdays(13)
        evenings = [row(day, [H], source="app", member="mem-a") for day in days]
        model = build(presence(days) + evenings)
        s = scorer(model)
        self.assertEqual(s.observed_days, 13)
        self.assertFalse(s.ready)
        self.assertGreater(s.score("majlis")[0], 0.6)
        self.assertIsNone(s.learned({"room_id": "majlis"}))
        # Weekend days count toward the two weeks too.
        weekend = [d for d in range(TODAY - 10, TODAY) if habits.weekday(d) in (4, 5)]
        model = build(presence(days + weekend) + evenings)
        self.assertTrue(scorer(model).ready)
        self.assertIsNotNone(scorer(model).learned({"room_id": "majlis"}))

    def test_days_nobody_was_home_are_not_counted_against_a_habit(self):
        days = workdays(30)
        used = days[:15]
        # On 15 days someone was home and the majlis was on at 20:00; on the
        # other 15 nothing happened anywhere (away, or the hub was off).
        model = build(
            presence(used) + [row(day, [H], source="app", member="m") for day in used]
        )
        self.assertEqual(scorer(model).observed_days, 15)
        habit, dates = scorer(model).score("majlis")
        self.assertEqual(dates, 15)
        self.assertGreater(habit, 0.6)


class VoteTests(unittest.TestCase):
    def test_one_vote_per_hour_and_neighbouring_hours_count_half(self):
        evidence = habits.RoomEvidence(full={TODAY - 1: (1 << 19) | (1 << 20)})
        self.assertEqual(evidence.near(TODAY - 1, 20, half_weight=True), 1.0)
        self.assertEqual(evidence.near(TODAY - 1, 21, half_weight=True), 0.5)
        self.assertEqual(evidence.near(TODAY - 1, 18, half_weight=True), 0.5)
        self.assertEqual(evidence.near(TODAY - 1, 22, half_weight=True), 0.0)

    def test_neighbouring_hours_run_across_midnight(self):
        evidence = habits.RoomEvidence(full={TODAY - 2: 1 << 23, TODAY - 4: 1 << 0})
        # 23:00 on one date is next to 00:00 on the next one, and back.
        self.assertEqual(evidence.near(TODAY - 1, 0, half_weight=True), 0.5)
        self.assertEqual(evidence.near(TODAY - 5, 23, half_weight=True), 0.5)
        self.assertEqual(evidence.near(TODAY - 3, 0, half_weight=True), 0.0)

    def test_wall_switch_votes_count_fully_and_only_for_the_household(self):
        days = workdays(20)
        rows = presence(days) + [row(day, [H]) for day in days]
        model = build(rows)
        majlis = model.household["majlis"]
        self.assertEqual(habits.SOURCE_WEIGHTS["wall_switch"], 1.0)
        self.assertEqual(majlis.half, {})
        self.assertEqual(set(majlis.full), set(days))
        habit, dates = scorer(model).score("majlis")
        self.assertEqual(dates, 20)
        app = build(
            presence(days)
            + [row(day, [H], source="app", member="mem-a") for day in days]
        )
        # The same evenings tapped in the app score the same for the house.
        self.assertEqual(scorer(app).score("majlis"), (habit, dates))
        self.assertEqual(model.members, {})

    def test_a_weight_under_one_is_kept_as_a_half_vote(self):
        # No source weighs 1/2 today; the model still keeps such votes apart.
        days = workdays(3)
        with mock.patch.dict(habits.SOURCE_WEIGHTS, {"wall_switch": 0.5}):
            model = build([row(days[0], [H])])
            self.assertEqual(model.household["majlis"].half, {days[0]: 1 << H})
            # A full vote the same hour wins over a half one.
            model = build([row(days[0], [H]), row(days[0], [H], source="ha_user")])
        self.assertEqual(
            model.household["majlis"].vote(days[0], H, half_weight=True), 1.0
        )

    def test_app_scene_and_home_assistant_users_count_fully(self):
        days = workdays(3)
        for source in ("app", "scene", "ha_user"):
            with self.subTest(source=source):
                model = build([row(days[0], [H], source=source, member="mem-a")])
                self.assertEqual(model.household["majlis"].full, {days[0]: 1 << H})

    def test_automations_and_unnamed_callers_weigh_nothing(self):
        days = workdays(20)
        for source in ("automation", "unknown"):
            with self.subTest(source=source):
                model = build([row(day, [H], source=source) for day in days])
                # Not a vote, and not a sign anyone was home either.
                self.assertEqual(model.household, {})
                self.assertEqual(scorer(model).observed_days, 0)

    def test_only_devices_that_make_a_room_on_vote(self):
        days = workdays(3)
        model = build(
            [
                row(days[0], [H], entity="switch.water_heater"),
                row(days[1], [H], entity="cover.majlis"),
            ]
        )
        majlis = model.household["majlis"]
        # Seen (someone was there), but no vote.
        self.assertEqual(majlis.seen, set(days[:2]))
        self.assertEqual((majlis.full, majlis.half), ({}, {}))


class WallSwitchWeightTests(unittest.TestCase):
    """Wall presses count at full weight for the household.

    In this market the wall switch is how most people switch a room on, so
    at half weight a wall-only room barely showed on workdays and never on
    the weekend.
    """

    def history(self, today, weekend_evenings=4):
        """Eight weeks before today. Someone is home every day (a kitchen
        press at 07:00) except one weekend away; the majlis is switched on
        at the wall at 20:00 on four workdays out of five (never on a
        Wednesday) and on weekend_evenings of every four weekend days."""
        rows, weekend = [], 0
        for day in range(today - 56, today):
            if habits.weekday(day) in (4, 5):
                weekend += 1
                if weekend in (5, 6):  # the third weekend away
                    continue
                if weekend % 4 < weekend_evenings:
                    rows.append(row(day, [H]))
            elif habits.weekday(day) != 2:
                rows.append(row(day, [H]))
            rows.append(row(day, [7], room="kitchen", entity="light.kitchen"))
        return rows

    def ask(self, today, **history):
        model = habits.build_model(
            self.history(today, **history),
            counted=COUNTED,
            self_scheduled=frozenset(),
            usable=habits.HabitSettings().usable_days(today, RIYADH),
        )
        s = scorer(model, today=today)
        habit, dates = s.score("majlis")
        return s, round(habit, 4), dates

    def test_a_wall_only_room_passes_on_workdays_and_weekends(self):
        thursday, friday, saturday = TODAY, TODAY + 1, TODAY + 2
        self.assertEqual(
            [habits.weekday(d) for d in (thursday, friday, saturday)], [3, 4, 5]
        )
        cases = (
            # (today, weekend evenings in 4, strength at 1, at 1/2, dates)
            (thursday, 4, 0.5995, 0.2997, 32),
            (friday, 4, 0.5230, 0.2615, 14),
            (saturday, 4, 0.5564, 0.2782, 14),
            # Fewer weekend evenings (3 in 4) still pass.
            (friday, 3, 0.3649, 0.1824, 10),
        )
        for today, evenings, full, half, dates in cases:
            with self.subTest(weekday=habits.weekday(today), evenings=evenings):
                s, habit, seen = self.ask(today, weekend_evenings=evenings)
                self.assertTrue(s.ready)
                self.assertEqual(s.observed_days, 54)
                self.assertEqual((habit, seen), (full, dates))
                self.assertGreaterEqual(habit, habits.MIN_STRENGTH)
                self.assertEqual(
                    s.learned({"room_id": "majlis"}), ("habit_now", {}, (0, -full))
                )
                # The same history at the old half weight never showed.
                with mock.patch.dict(habits.SOURCE_WEIGHTS, {"wall_switch": 0.5}):
                    s, habit, _ = self.ask(today, weekend_evenings=evenings)
                self.assertEqual(habit, half)
                self.assertIsNone(s.learned({"room_id": "majlis"}))

    def test_wall_presses_never_reach_a_persons_layer(self):
        days = workdays(25)
        rows = presence(days) + [row(day, [H]) for day in days]
        # Member A taps the kitchen on from the app; a wall row that somehow
        # carried a member id still isn't theirs.
        rows += [
            row(day, [H], room="kitchen", entity="light.kitchen", source="app",
                member="mem-a")
            for day in days[:20]
        ]  # fmt: skip
        rows.append(row(days[0], [9], member="mem-a"))
        model = build(rows)
        self.assertEqual(set(model.members), {"mem-a"})
        own = model.members["mem-a"]
        self.assertEqual(set(own), {"kitchen"})
        self.assertEqual(own["kitchen"].vote_dates(), set(days[:20]))
        # The household has every wall press; A's own layer none, so for A
        # the majlis' wall habit is narrowed by their own taps (lambda 2/3).
        self.assertEqual(set(model.household["majlis"].full), set(days))
        household = scorer(model).score("majlis")[0]
        mine = scorer(model, member="mem-a")
        self.assertAlmostEqual(mine.personal_weight, 20 / 30)
        self.assertAlmostEqual(mine.score("majlis")[0], household / 3)
        # A member with no app taps gets the household's answer unchanged.
        self.assertEqual(scorer(model, member="mem-b").score("majlis")[0], household)


class ScheduleOptOutTests(unittest.TestCase):
    def test_a_device_on_its_own_schedule_stops_voting_from_the_wall(self):
        days = workdays(39)
        rows = presence(days) + [row(day, [H]) for day in days]
        rows.append(row(days[0], [H - 2], source="app", member="mem-a"))
        model = build(rows, scheduled={"light.majlis"})
        majlis = model.household["majlis"]
        # Its schedule-driven "wall" ons are gone, and aren't presence either;
        # a person's app tap on the same light still counts.
        self.assertEqual(majlis.half, {})
        self.assertEqual(majlis.full, {days[0]: 1 << (H - 2)})
        self.assertEqual(majlis.seen, {days[0]})
        self.assertIsNone(scorer(model).learned({"room_id": "majlis"}))
        self.assertIsNotNone(
            scorer(build(rows, counted=COUNTED)).learned({"room_id": "majlis"})
        )


class DayClassTests(unittest.TestCase):
    def setUp(self):
        weeks = range(TODAY - 56, TODAY)
        self.weekend = [d for d in weeks if habits.weekday(d) in (4, 5)]
        self.work = [d for d in weeks if habits.weekday(d) not in (4, 5)]
        # On the weekend the majlis is on at 20:00; on workdays it isn't.
        self.rows = presence(self.weekend + self.work) + [
            row(day, [H], source="app", member="mem-a") for day in self.weekend
        ]

    def test_saudi_weekend_is_friday_and_saturday_by_default(self):
        self.assertEqual(habits.HabitSettings().weekend_days, (4, 5))
        model = build(self.rows)
        friday = TODAY + 1
        self.assertEqual(habits.weekday(friday), 4)
        self.assertIsNone(scorer(model).learned({"room_id": "majlis"}))  # Thursday
        self.assertIsNotNone(scorer(model, today=friday).learned({"room_id": "majlis"}))
        sunday = TODAY + 3
        self.assertIsNone(scorer(model, today=sunday).learned({"room_id": "majlis"}))

    def test_the_weekend_is_configurable_without_a_rebuild(self):
        # The same model, asked on a Friday: with a Saturday-Sunday weekend,
        # Friday is a workday, and workday evenings were never on.
        model = build(self.rows)
        friday = TODAY + 1
        fri_sat = scorer(model, today=friday, weekend=(4, 5))
        sat_sun = scorer(model, today=friday, weekend=(5, 6))
        self.assertIsNotNone(fri_sat.learned({"room_id": "majlis"}))
        self.assertIsNone(sat_sun.learned({"room_id": "majlis"}))
        self.assertLess(sat_sun.score("majlis")[0], fri_sat.score("majlis")[0])


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.days = workdays(25)
        # The household turns the majlis on at 20:00 every evening (wall);
        # member A taps the kitchen on at 20:00 from the app most evenings;
        # member B never uses the app.
        self.rows = presence(self.days) + [row(day, [H]) for day in self.days]
        self.rows += [
            row(day, [H], room="kitchen", entity="light.kitchen", source="app",
                member="mem-a")
            for day in self.days[:20]
        ]  # fmt: skip

    def test_household_learns_every_person_and_the_wall(self):
        s = scorer(build(self.rows))
        self.assertIsNotNone(s.learned({"room_id": "majlis"}))
        self.assertIsNotNone(s.learned({"room_id": "kitchen"}))

    def test_a_member_layer_is_their_own_app_taps(self):
        model = build(
            self.rows + [row(self.days[0], [9], source="scene", member="mem-a")]
        )
        own = model.members["mem-a"]
        self.assertEqual(set(own), {"kitchen"})
        self.assertEqual(own["kitchen"].vote_dates(), set(self.days[:20]))
        self.assertNotIn("mem-b", model.members)

    def test_a_phone_blends_its_member_over_the_household(self):
        model = build(self.rows)
        household = scorer(model)
        a = scorer(model, member="mem-a")
        b = scorer(model, member="mem-b")
        # A's own taps say kitchen, not majlis: the majlis' household habit
        # is narrowed for A (lambda = 20 / 30), not for B, who has no history.
        self.assertAlmostEqual(a.personal_weight, 20 / 30)
        self.assertEqual(b.personal_weight, 0)
        self.assertIsNotNone(a.learned({"room_id": "kitchen"}))
        self.assertIsNone(a.learned({"room_id": "majlis"}))
        self.assertLess(a.score("majlis")[0], household.score("majlis")[0])
        self.assertEqual(b.score("majlis"), household.score("majlis"))
        self.assertIsNotNone(b.learned({"room_id": "majlis"}))

    def test_the_personal_weight(self):
        self.assertEqual(habits.personal_weight(0), 0)
        self.assertEqual(habits.personal_weight(10), 0.5)
        self.assertEqual(habits.personal_weight(40), 0.8)
        self.assertEqual(habits.personal_weight(1000), 0.8)

    def test_a_scoped_member_sees_nothing_from_rooms_outside_their_scope(self):
        base = build(self.rows)
        # Activity only in the kitchen and a third room: none of it may change
        # what a member who only has the majlis gets.
        busier = build(
            self.rows
            + [
                row(d, [H, 7], room="office", entity="light.kitchen")
                for d in workdays(50)
            ]
            + [
                row(d, [3], room="kitchen", entity="light.kitchen")
                for d in workdays(50)
            ]
        )
        for member in (None, "mem-a", "mem-b"):
            with self.subTest(member=member):
                before = scorer(base, ["majlis"], member=member)
                after = scorer(busier, ["majlis"], member=member)
                self.assertEqual(before.observed_days, after.observed_days)
                self.assertEqual(before.score("majlis"), after.score("majlis"))
                self.assertEqual(
                    before.learned({"room_id": "majlis"}),
                    after.learned({"room_id": "majlis"}),
                )


class StorageShapeTests(unittest.TestCase):
    def test_a_layer_round_trips_through_json(self):
        model = build(
            presence(workdays(20))
            + [row(day, [H, 6]) for day in workdays(20)]
            + [row(workdays(2)[1], [H], source="app", member="mem-a")]
        )
        for layer in (model.household, model.members["mem-a"]):
            doc = json.loads(json.dumps(habits.layer_to_doc(layer)))
            self.assertEqual(habits.layer_from_doc(doc), layer)

    def test_a_malformed_layer_is_refused(self):
        for bad in (
            [],
            {"majlis": []},
            {"majlis": {"seen": [1]}},
            {"majlis": {"seen": [-1], "on": []}},
            {"majlis": {"seen": [], "on": [[1, 2]]}},
            {"majlis": {"seen": [], "on": [[1, True, 0]]}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                habits.layer_from_doc(bad)

    def test_a_busy_villa_stays_small(self):
        # 30 rooms on every hour of every date of the window: the largest a
        # layer can get.
        rows = [
            row(day, range(24), room=f"room-{n}", entity="light.majlis",
                source=source)
            for n in range(30)
            for day in range(TODAY - 56, TODAY)
            for source in ("app", "wall_switch")
        ]  # fmt: skip
        doc = habits.layer_to_doc(build(rows).household)
        size = len(json.dumps(doc, separators=(",", ":")))
        self.assertLess(size, 30 * 56 * 40)
        self.assertEqual(len(doc), 30)

    def test_the_signature_is_stable_and_order_free(self):
        a = habits.signature(["b", "a"], [("x", 1)])
        self.assertEqual(a, habits.signature(["a", "b"], [("x", 1)]))
        self.assertNotEqual(a, habits.signature(["a"], [("x", 1)]))
        self.assertEqual(len(a), 16)


class SettingsTests(unittest.TestCase):
    MANAGED = {"light.majlis", "switch.heater"}

    def update(self, settings, payload, now_t=1_800_000_000):
        return settings.updated(payload, now_t, managed_ids=self.MANAGED)

    def test_defaults(self):
        settings = habits.HabitSettings()
        self.assertEqual(
            settings.to_doc(),
            {
                "learning": True,
                "paused": False,
                "weekend_days": [4, 5],
                "self_scheduled_entity_ids": [],
                "reset_at": None,
                "excluded": [],
            },
        )
        self.assertEqual(settings.status, "learning")
        self.assertEqual(habits.HabitSettings.from_doc(None), settings)
        self.assertEqual(habits.HabitSettings.from_doc(settings.to_doc()), settings)

    def test_bad_values_are_refused(self):
        settings = habits.HabitSettings()
        for bad in (
            [],
            {"learn": True},
            {"learning": "yes"},
            {"paused": 1},
            {"weekend_days": [7]},
            {"weekend_days": [True]},
            {"weekend_days": "fri"},
            {"self_scheduled_entity_ids": "light.majlis"},
            {"self_scheduled_entity_ids": ["light.unknown"]},
            {"self_scheduled_entity_ids": [3]},
            {"self_scheduled_entity_ids": ["light.majlis"] * 257},
        ):
            with self.subTest(bad=bad), self.assertRaises(habits.HabitSettingsError):
                self.update(settings, bad)

    def test_values_are_cleaned(self):
        settings = self.update(
            habits.HabitSettings(),
            {
                "weekend_days": [6, 5, 5],
                "self_scheduled_entity_ids": ["switch.heater", "switch.heater"],
            },
        )
        self.assertEqual(settings.weekend_days, (5, 6))
        self.assertEqual(settings.self_scheduled_entity_ids, ("switch.heater",))
        # A malformed stored field keeps its default; the rest survive.
        doc = {**settings.to_doc(), "weekend_days": "x", "learning": "no"}
        stored = habits.HabitSettings.from_doc(doc)
        self.assertEqual(stored.weekend_days, (4, 5))
        self.assertTrue(stored.learning)
        self.assertEqual(stored.self_scheduled_entity_ids, ("switch.heater",))

    def test_pausing_leaves_its_dates_out(self):
        t0 = (TODAY - 10) * 86400 - RIYADH * 60 + 15 * 3600  # 15:00 local
        settings = self.update(habits.HabitSettings(), {"paused": True}, t0)
        self.assertEqual(settings.status, "paused")
        self.assertEqual(settings.excluded, ((t0, None),))
        # Still paused today: every date from the pause on is left out.
        usable = settings.usable_days(TODAY, RIYADH)
        self.assertTrue(usable(TODAY - 11))
        self.assertFalse(usable(TODAY - 10))
        self.assertFalse(usable(TODAY - 1))
        t1 = t0 + 3 * 86400  # resumed three days later, 15:00
        settings = self.update(settings, {"paused": False}, t1)
        self.assertEqual(settings.excluded, ((t0, t1),))
        usable = settings.usable_days(TODAY, RIYADH)
        self.assertEqual(
            [d for d in range(TODAY - 12, TODAY) if not usable(d)],
            [TODAY - 10, TODAY - 9, TODAY - 8, TODAY - 7],
        )
        # Pausing again opens a second period; an off while paused keeps it.
        settings = self.update(settings, {"paused": True}, t1 + 86400)
        settings = self.update(settings, {"learning": False}, t1 + 2 * 86400)
        self.assertEqual(settings.status, "off")
        self.assertEqual(settings.excluded, ((t0, t1), (t1 + 86400, None)))
        settings = self.update(
            settings, {"learning": True, "paused": False}, t1 + 3 * 86400
        )
        self.assertEqual(settings.excluded[-1], (t1 + 86400, t1 + 3 * 86400))

    def test_old_periods_go_once_no_date_in_them_can_be_read(self):
        # Kept as long as the log keeps human rows (400 days): the regular
        # window reaches back past a Ramadan, and a finished Ramadan is only
        # copied out of the log when the hub next learns.
        now = TODAY * 86400
        recent = (now - 90 * 86400, now - 80 * 86400)
        old = (now - 420 * 86400, now - 410 * 86400)
        settings = self.update(
            habits.HabitSettings(excluded=(old, recent)), {"weekend_days": [4, 5]}, now
        )
        self.assertEqual(settings.excluded, (recent,))

    def test_reset_forgets_everything_before_it(self):
        t = (TODAY - 5) * 86400 - RIYADH * 60 + 10 * 3600  # 10:00 local
        settings = self.update(habits.HabitSettings(), {"paused": True}, t - 86400)
        settings = settings.reset(t)
        # Still paused: a new period starts at the reset; old ones are gone.
        self.assertEqual(settings.reset_at, t)
        self.assertEqual(settings.excluded, ((t, None),))
        usable = habits.HabitSettings().reset(t).usable_days(TODAY, RIYADH)
        # The reset date itself is only half observed: learning starts the
        # next day.
        self.assertEqual(usable.first, TODAY - 4)
        model = build(
            [row(day, [H]) for day in range(TODAY - 10, TODAY)], usable=usable
        )
        self.assertEqual(
            sorted(model.household["majlis"].full), list(range(TODAY - 4, TODAY))
        )

    def test_the_model_key_ignores_the_weekend(self):
        settings = habits.HabitSettings()
        weekend = self.update(settings, {"weekend_days": [5, 6]})
        self.assertEqual(settings.model_key(), weekend.model_key())
        scheduled = self.update(
            settings, {"self_scheduled_entity_ids": ["switch.heater"]}
        )
        self.assertNotEqual(settings.model_key(), scheduled.model_key())


# Ramadan 1448 by Umm al-Qura: 2027-02-08 (Monday) to 2027-03-08 (Monday);
# 1 Shawwal (Eid) is 2027-03-09, a Tuesday. Ramadan 1447: 2026-02-18 to
# 2026-03-19; 1446: 2025-03-01 to 2025-03-29.
SHABAN_29 = 20856  # 2027-02-07, the eve of Ramadan 1448
RAMADAN_1 = 20857
RAMADAN_LAST = 20885
EID = 20886
LAST_RAMADAN = range(20502, 20532)  # 1447
RAMADAN_1446 = range(20148, 20177)


def daily(days, hours, **kwargs):
    return [row(day, hours, **kwargs) for day in days]


class RamadanProfileTests(unittest.TestCase):
    """Ramadan is detected by itself (Umm al-Qura),
    learned apart for its 30 days, and the pre-Ramadan model is back after
    it; the Ramadan profile is kept for the next year."""

    def usable(self, today, profile=None, settings=None):
        return (settings or habits.HabitSettings()).usable_days(today, RIYADH, profile)

    def model(self, rows, today, profile=None):
        usable = self.usable(today, profile)
        model = habits.build_model(
            rows, counted=COUNTED, self_scheduled=frozenset(), usable=usable
        )
        model.ages = usable.ages
        return model

    def test_each_date_has_a_profile(self):
        self.assertEqual(habits.weekday(RAMADAN_1), 0)  # Monday
        for day, profile in (
            (SHABAN_29, "regular"),
            (RAMADAN_1, "ramadan"),
            (RAMADAN_LAST, "ramadan"),
            (EID, "regular"),
            (TODAY, "regular"),
        ):
            with self.subTest(day=day):
                self.assertEqual(habits.profile_of(day), profile)
                self.assertEqual(self.usable(day).profile, profile)

    def test_the_regular_window_steps_over_ramadan(self):
        window = habits.profile_window(EID, "regular")
        self.assertEqual(len(window), 56)
        self.assertEqual(window[0], SHABAN_29)
        self.assertEqual(window[-1], SHABAN_29 - 55)
        self.assertFalse(set(window) & set(range(RAMADAN_1, EID)))
        # With no Ramadan in it, it is the 56 dates before today (as before).
        self.assertEqual(
            habits.profile_window(TODAY, "regular"), list(range(TODAY - 1, TODAY - 57, -1))
        )  # fmt: skip

    def test_the_ramadan_window_reaches_back_to_last_ramadan_only(self):
        first = habits.profile_window(RAMADAN_1, "ramadan")
        # On 1 Ramadan: last year's 30 days, its last day the newest. The
        # one before (1446) is older than the log's 400 days: never reached,
        # so no one's activity is learned from for longer than that.
        self.assertEqual(first, list(reversed(LAST_RAMADAN)))
        self.assertGreater(RAMADAN_1 - RAMADAN_1446[-1], 400)
        usable = self.usable(RAMADAN_1)
        self.assertEqual(usable.ages[LAST_RAMADAN[-1]], 1)
        self.assertEqual(usable.ages[LAST_RAMADAN[0]], 30)
        self.assertEqual(usable.spans(), [(LAST_RAMADAN[0], LAST_RAMADAN[-1])])
        # On its last day it still holds all of last year's (384 days back).
        last = habits.profile_window(RAMADAN_LAST, "ramadan")
        self.assertEqual(len(last), 56)
        self.assertEqual(last[28:], list(reversed(LAST_RAMADAN))[:28])
        # On day 10: this Ramadan's nine days first.
        tenth = habits.profile_window(RAMADAN_1 + 9, "ramadan")
        self.assertEqual(tenth[:9], list(range(RAMADAN_1 + 8, RAMADAN_1 - 1, -1)))
        self.assertEqual(tenth[9], LAST_RAMADAN[-1])

    def test_each_profile_learns_only_its_own_dates(self):
        rows = daily(range(SHABAN_29 - 80, EID + 20), [H])
        regular = self.model(rows, EID)
        ramadan = self.model(rows, RAMADAN_LAST)
        self.assertFalse(
            set(regular.household["majlis"].seen) & set(range(RAMADAN_1, EID))
        )
        self.assertEqual(
            set(ramadan.household["majlis"].seen), set(range(RAMADAN_1, RAMADAN_LAST))
        )

    def test_the_regular_model_is_frozen_on_1_ramadan_and_back_on_1_shawwal(self):
        # Before Ramadan the majlis is on at 20:00 on workdays; during
        # Ramadan at 21:00 (after Taraweeh) every day.
        before = [
            d
            for d in range(SHABAN_29 - 70, RAMADAN_1)
            if habits.weekday(d) not in (4, 5)
        ]
        rows = daily(before, [H]) + daily(range(RAMADAN_1, EID), [21, 2])
        rows += presence(range(SHABAN_29 - 70, EID))
        on_1_ramadan = self.usable(RAMADAN_1, "regular")
        on_eid = self.usable(EID, "regular")
        self.assertEqual(
            (on_eid.first, on_eid.last, dict(on_eid.ages)),
            (on_1_ramadan.first, on_1_ramadan.last, dict(on_1_ramadan.ages)),
        )
        frozen = self.model(rows, RAMADAN_1, "regular")
        back = self.model(rows, EID)
        self.assertEqual(frozen, back)
        # Monday 1 Ramadan and Tuesday Eid are both workdays: the same answer.
        self.assertEqual(
            scorer(frozen, today=RAMADAN_1).score("majlis"),
            scorer(back, today=EID).score("majlis"),
        )
        self.assertIsNotNone(scorer(back, today=EID).learned({"room_id": "majlis"}))
        # Ramadan's suhoor at 02:00 never reached it.
        self.assertIsNone(
            scorer(back, today=EID, hour=2).learned({"room_id": "majlis"})
        )
        # Aged by the calendar instead, Ramadan's 29 days would have faded it.
        calendar = scorer(habits.HabitModel(back.household), today=EID)
        self.assertLess(
            calendar.score("majlis")[0], scorer(back, today=EID).score("majlis")[0]
        )

    def test_last_years_ramadan_answers_from_the_first_day(self):
        # Ramadan 1447: the majlis on at 21:00 every day; the rest of the
        # year at 20:00 on workdays.
        rows = daily(LAST_RAMADAN, [21]) + presence(LAST_RAMADAN)
        rows += daily(range(RAMADAN_1 - 60, RAMADAN_1), [H]) + presence(
            range(RAMADAN_1 - 60, RAMADAN_1)
        )
        ramadan = self.model(rows, RAMADAN_1)
        s = scorer(ramadan, today=RAMADAN_1, hour=21)
        self.assertTrue(s.ready)
        self.assertEqual(s.observed_days, 30)
        self.assertIsNotNone(s.learned({"room_id": "majlis"}))
        # The regular evening isn't a Ramadan habit.
        self.assertIsNone(
            scorer(ramadan, today=RAMADAN_1, hour=19).learned({"room_id": "majlis"})
        )

    def test_this_ramadan_takes_over_from_the_last(self):
        # Last Ramadan the majlis was on at 21:00; this one at 23:00. Last
        # year's 30 days are evidence like any other, so the change takes
        # about as long as any changed habit (about 10 days).
        rows = daily(LAST_RAMADAN, [21]) + presence(LAST_RAMADAN)
        this = range(RAMADAN_1, RAMADAN_LAST + 1)
        rows += daily(this, [23]) + presence(this)
        shows = {21: [], 23: []}
        for n in range(29):
            today = RAMADAN_1 + n
            if habits.weekday(today) in (4, 5):
                continue  # weekends have fewer dates of their own; see below
            model = self.model(rows, today)
            for hour in shows:
                learned = scorer(model, today=today, hour=hour).learned(
                    {"room_id": "majlis"}
                )
                shows[hour].append((n + 1, learned is not None))
        # On workdays: 21:00 from day 1 to day 14; 23:00 from day 11 on.
        self.assertEqual(
            [d for d, on in shows[21] if on], [1, 2, 3, 4, 7, 8, 9, 10, 11, 14]
        )
        self.assertEqual(
            [d for d, on in shows[23] if not on], [1, 2, 3, 4, 7, 8, 9, 10]
        )

    def test_pauses_and_resets_block_dates_in_either_profile(self):
        t = (RAMADAN_1 + 4) * 86400 - RIYADH * 60 + 15 * 3600  # day 5, 15:00
        settings = habits.HabitSettings(excluded=((t, t + 2 * 86400),))
        blocked = settings.blocked_days(range(RAMADAN_1, EID), RIYADH)
        self.assertEqual(blocked, {RAMADAN_1 + 4, RAMADAN_1 + 5, RAMADAN_1 + 6})
        usable = self.usable(RAMADAN_1 + 10, settings=settings)
        self.assertEqual(
            [d for d in range(RAMADAN_1, RAMADAN_1 + 10) if not usable(d)],
            [RAMADAN_1 + 4, RAMADAN_1 + 5, RAMADAN_1 + 6],
        )
        reset = habits.HabitSettings().reset(t)
        self.assertEqual(
            reset.blocked_days(range(RAMADAN_1, EID), RIYADH),
            set(range(RAMADAN_1, RAMADAN_1 + 5)),
        )
        # Nothing of last Ramadan counts after a reset this one.
        self.assertFalse(
            any(self.usable(RAMADAN_1 + 10, settings=reset)(d) for d in LAST_RAMADAN)
        )


class LocalDayTests(unittest.TestCase):
    def test_riyadh_dates_turn_at_local_midnight(self):
        # 2026-10-22 20:59:59 UTC is 23:59:59 in Riyadh; a second later it is
        # 2026-10-23 there.
        t = 1792702799
        self.assertEqual(habits.local_day(t, RIYADH), TODAY)
        self.assertEqual(habits.local_day(t + 1, RIYADH), TODAY + 1)
        self.assertEqual(habits.local_day(t + 1, 0), TODAY)
        self.assertEqual(habits.weekday(TODAY + 1), 4)  # Friday


if __name__ == "__main__":
    unittest.main()
