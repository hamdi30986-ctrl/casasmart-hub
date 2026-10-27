"""room_intel.habits: each device's off votes and "usually off by now" (no HA).

The fixture of test_room_habits: hand-built habit_rows rows (now with
off_hours), the real model, scorer and storage shape.
"""

import json
import unittest

from test_room_habits import COUNTED, TODAY, build, habits, presence, workdays
from test_room_habits import scorer as scorer_at

OFF_H = 23  # the majlis goes dark at 23:xx


def row(
    day,
    *,
    on=(),
    off=(),
    room="majlis",
    entity="light.majlis",
    source="wall_switch",
    member=None,
):
    """A habit_rows row with these on and off hours."""
    return {
        "room_id": room,
        "entity_id": entity,
        "source": source,
        "member_id": member,
        "local_day": day,
        "on_hours": sum(1 << h for h in set(on)),
        "off_hours": sum(1 << h for h in set(off)),
    }


def mask(*hours):
    return sum(1 << h for h in hours)


def scorer(model, rooms=("majlis", "kitchen"), *, hour=OFF_H, **kwargs):
    """test_room_habits' scorer, at OFF_H by default."""
    return scorer_at(model, rooms, hour=hour, **kwargs)


class OffVotesTests(unittest.TestCase):
    """x_d of an off habit: an off this hour or the hour before, unless it
    was switched on this hour."""

    D = TODAY - 1

    def x(self, off, on=None, *, hour=OFF_H, half_weight=True, day=D):
        votes = habits.OffVotes(
            habits.Votes(full=off), None if on is None else habits.Votes(full=on)
        )
        return votes.near(day, hour, half_weight=half_weight)

    def test_an_off_this_hour_or_the_hour_before_counts_in_full(self):
        self.assertEqual(self.x({self.D: mask(OFF_H)}), 1.0)
        self.assertEqual(self.x({self.D: mask(OFF_H - 1)}), 1.0)
        # Never before the usual time, nor two hours late.
        self.assertEqual(self.x({self.D: mask(OFF_H + 1)}), 0.0)
        self.assertEqual(self.x({self.D: mask(OFF_H - 2)}), 0.0)
        self.assertEqual(self.x({}), 0.0)

    def test_the_hour_before_runs_across_midnight(self):
        self.assertEqual(self.x({self.D - 1: mask(23)}, hour=0), 1.0)
        self.assertEqual(self.x({self.D: mask(23)}, hour=0), 0.0)

    def test_switched_on_this_hour_means_in_use_not_off(self):
        # On and off within the hour (a bathroom light): it was in use.
        self.assertEqual(self.x({self.D: mask(OFF_H)}, {self.D: mask(OFF_H)}), 0.0)
        self.assertEqual(self.x({self.D: mask(OFF_H - 1)}, {self.D: mask(OFF_H)}), 0.0)
        # Used briefly the hour before (on and off then): off by now.
        self.assertEqual(
            self.x({self.D: mask(OFF_H - 1)}, {self.D: mask(OFF_H - 1)}), 1.0
        )
        # On earlier in the evening: no matter.
        self.assertEqual(self.x({self.D: mask(OFF_H)}, {self.D: mask(19)}), 1.0)

    def test_half_votes_count_half_and_only_when_asked_for(self):
        votes = habits.OffVotes(habits.Votes(half={self.D: mask(OFF_H)}))
        self.assertEqual(votes.near(self.D, OFF_H, half_weight=True), 0.5)
        self.assertEqual(votes.near(self.D, OFF_H, half_weight=False), 0.0)
        # A half "on" this hour is still an on.
        votes = habits.OffVotes(
            habits.Votes(full={self.D: mask(OFF_H)}),
            habits.Votes(half={self.D: mask(OFF_H)}),
        )
        self.assertEqual(votes.near(self.D, OFF_H, half_weight=True), 0.0)


class OffModelTests(unittest.TestCase):
    def setUp(self):
        self.days = workdays(25)
        rows = presence(self.days)
        # The majlis light: on at 19:00, off at 23:00 at the wall, every
        # evening. The AC switched off with it on 20 evenings, from the app.
        rows += [row(day, on=[19], off=[OFF_H]) for day in self.days]
        rows += [
            row(
                day,
                on=[19],
                off=[OFF_H],
                entity="climate.majlis",
                source="app",
                member="mem-a",
            )
            for day in self.days[:20]
        ]
        self.rows = rows
        self.model = build(rows)

    def off(self, room="majlis", **kwargs):
        return scorer(self.model, **kwargs).usual_off_devices(room)

    def test_offs_vote_for_the_device_only(self):
        majlis = self.model.household["majlis"]
        self.assertEqual(set(majlis.offs), {"light.majlis", "climate.majlis"})
        self.assertEqual(majlis.offs["light.majlis"].full[self.days[0]], mask(OFF_H))
        # The room's votes are its "on"s: the same strength with or without
        # the offs, so the ranking and usually_on_now don't move.
        without = build([{**r, "off_hours": 0} for r in self.rows]).household["majlis"]
        self.assertEqual((majlis.full, majlis.half), (without.full, without.half))
        self.assertEqual(without.offs, {})
        for hour in (19, 20, OFF_H, 0):
            self.assertEqual(
                scorer(self.model, hour=hour).score("majlis"),
                scorer(
                    build([{**r, "off_hours": 0} for r in self.rows]), hour=hour
                ).score("majlis"),
            )
        # A row read without off_hours (a version 1 kept Ramadan) has none.
        legacy = {
            k: v for k, v in row(self.days[0], on=[19]).items() if k != "off_hours"
        }
        self.assertEqual(build([legacy]).household["majlis"].offs, {})

    def test_usually_off_by_now_from_the_usual_hour_for_two_hours(self):
        # Strongest first: the light every evening, the AC on 20 of 25.
        self.assertEqual(self.off(), ["light.majlis", "climate.majlis"])
        self.assertEqual(self.off(hour=0), ["light.majlis", "climate.majlis"])
        # Not before the usual time, not long after it, not where it's never.
        for hour in (19, 22, 1):
            self.assertEqual(self.off(hour=hour), [], hour)
        self.assertEqual(self.off("kitchen"), [])
        # The same bar as the "on"s: 0.35 on at least 3 dates.
        habit, dates = scorer(self.model)._blend(
            self.model.household["majlis"].off_votes()["light.majlis"], None
        )
        self.assertGreater(habit, habits.MIN_STRENGTH)
        self.assertEqual(dates, 25)

    def test_the_bar_needs_most_evenings_and_three_dates(self):
        # As for the "on"s (test_room_habits.test_strength_threshold): the
        # newest 7 of 20 observed evenings.
        days = workdays(20)
        for evenings, usual in ((6, False), (7, True)):
            model = build(
                presence(days) + [row(day, off=[OFF_H]) for day in days[:evenings]]
            )
            self.assertEqual(
                scorer(model).usual_off_devices("majlis"),
                ["light.majlis"] if usual else [],
                evenings,
            )
        # Two dates are never a habit, however strong.
        days = workdays(14)
        model = build(presence(days) + [row(day, off=[OFF_H]) for day in days[:2]])
        self.assertEqual(scorer(model).usual_off_devices("majlis"), [])

    def test_a_device_toggled_within_the_hour_is_not_usually_off(self):
        days = workdays(25)
        model = build(
            presence(days)
            + [
                row(
                    day, on=[OFF_H], off=[OFF_H], room="kitchen", entity="light.kitchen"
                )
                for day in days
            ]
        )
        self.assertEqual(scorer(model).usual_off_devices("kitchen"), [])
        # An hour later it is: the last thing done to it was an off.
        self.assertEqual(
            scorer(model, hour=0).usual_off_devices("kitchen"), ["light.kitchen"]
        )

    def test_layers_weights_and_opt_outs_as_for_the_ons(self):
        # The wall-pressed light is the household's, never member A's own.
        mine = scorer(self.model, member="mem-a")
        self.assertEqual(mine.usual_off_devices("majlis"), ["climate.majlis"])
        self.assertNotIn("light.majlis", self.model.members["mem-a"]["majlis"].offs)
        # Automations and unnamed callers weigh nothing; a device that never
        # makes a room on (a plug) doesn't vote; nor does a device on its own
        # schedule from the wall.
        days = self.days
        rows = presence(days)
        rows += [row(day, off=[OFF_H], source="automation") for day in days]
        rows += [row(day, off=[OFF_H], source="unknown") for day in days]
        rows += [row(day, off=[OFF_H], entity="switch.plug") for day in days]
        rows += [row(day, off=[OFF_H], entity="climate.majlis") for day in days]
        model = habits.build_model(
            rows,
            counted=COUNTED,
            self_scheduled=frozenset({"climate.majlis"}),
            usable=habits.HabitSettings().usable_days(TODAY, 180),
        )
        self.assertEqual(model.household["majlis"].offs, {})

    def test_nothing_is_usually_off_before_two_weeks_of_observed_days(self):
        days = workdays(13)
        model = build(presence(days) + [row(day, off=[OFF_H]) for day in days])
        s = scorer(model)
        self.assertFalse(s.ready)
        self.assertEqual(s.usual_off_devices("majlis"), [])

    def test_offs_round_trip_and_stay_small(self):
        for layer in (self.model.household, self.model.members["mem-a"]):
            doc = json.loads(json.dumps(habits.layer_to_doc(layer)))
            self.assertEqual(habits.layer_from_doc(doc), layer)
        doc = habits.layer_to_doc(build([row(self.days[0], off=[OFF_H])]).household)
        self.assertEqual(
            doc["majlis"]["offs"], {"light.majlis": [[self.days[0], mask(OFF_H), 0]]}
        )
        # 30 rooms switched on and off at every hour of every date of the
        # window: the largest a layer can get, at most twice the "on"-only
        # bound (test_room_habits.test_a_busy_villa_stays_small).
        rows = [
            row(day, on=range(24), off=range(24), room=f"room-{n}", source=source)
            for n in range(30)
            for day in range(TODAY - 56, TODAY)
            for source in ("app", "wall_switch")
        ]
        doc = habits.layer_to_doc(build(rows).household)
        self.assertLess(len(json.dumps(doc, separators=(",", ":"))), 2 * 30 * 56 * 40)


if __name__ == "__main__":
    unittest.main()
