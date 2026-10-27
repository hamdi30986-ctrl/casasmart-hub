"""?habits_only=1: the habit kinds once the habits are ready, the room plans
before.

The fixtures of test_generated_habit_suggestions (P1) and
test_generated_habit_off_suggestions (P3): the real suggestion handlers,
runtime, scene executor, ranking and HabitLearner over a real SQLite room
activity log. Home Assistant's states are fakes.
"""

from __future__ import annotations

import importlib
import unittest
from datetime import UTC, datetime
from unittest import mock

import test_generated_habit_suggestions as p1

TODAY, H, NOW = p1.TODAY, p1.H, p1.NOW
generated = importlib.import_module("phase4_fixture.generated_suggestions")
habits_module = importlib.import_module("phase4_fixture.room_intel.habits")
capabilities = importlib.import_module("phase4_fixture.capabilities")
suggestions_module = importlib.import_module("phase4_fixture.suggestions")

EVERY = "room_off,room_eco,usually_on_now,usually_off_now"
# What the P4b tablet and phone ask for, and what a P3/P4 app asked for.
TABLET = {"kinds": EVERY, "habits": "household", "habits_only": "1"}
PHONE = {"kinds": EVERY, "habits_only": "1"}
MIXED = {"kinds": EVERY, "habits": "household"}
DAYS = range(TODAY - 56, TODAY)


def offered(payload):
    return [(p["kind"], p["room_id"]) for p in payload["suggestions"]]


class Case(p1.HabitSuggestionCase):
    async def answer(self, query, **claims):
        """(habits_ready, [(kind, room)]) for a read."""
        payload = await self.get(query, **claims)
        return payload.get("habits_ready"), offered(payload)

    def log(self, events):
        self.db.room_activity().append_many(events)

    def someone_home(self, days=DAYS):
        """The kitchen light switched on every day, a different hour each day:
        dates the hub observes, and no habit."""
        self.log([p1.event(day, day % 24, "light.kitchen") for day in days])

    def majlis_evenings(self, entities=("light.majlis",), days=DAYS):
        self.log([p1.event(day, H, e) for day in days for e in entities])


class AskTest(unittest.TestCase):
    def test_habits_only_is_1_or_0(self):
        ask = generated.ask_from_query
        self.assertEqual(ask({"habits_only": "1"}), generated.Ask(habits_only=True))
        self.assertEqual(ask({"habits_only": "0"}), generated.Ask())
        self.assertEqual(ask({}), generated.Ask())
        self.assertFalse(ask({"kinds": EVERY}).habits_only)
        for value in ("true", "", "2", "yes", "1 "):
            with (
                self.subTest(value=value),
                self.assertRaises(suggestions_module.SuggestionError) as caught,
            ):
                ask({"habits_only": value})
            self.assertEqual(caught.exception.code, "invalid_habits_only")


class ContractTest(Case):
    async def test_a_bad_value_is_a_400(self):
        response = await self.view.get(
            self.request(query={**TABLET, "habits_only": "true"})
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.data["error"], "invalid_habits_only")
        self.assertTrue(response.data["message"])

    async def test_the_handshake_advertises_it(self):
        feature = capabilities.handshake_capabilities()["features"][
            "generated_room_habits_only_v1"
        ]
        self.assertEqual(
            feature, {"version": 1, "minimum_api_version": 1, "available": True}
        )

    async def test_without_it_the_answer_is_the_one_before_it(self):
        self.history()
        for query in ({}, {"kinds": "room_off,room_eco"}, MIXED,
                      {**p1.ALL_KINDS, "habits": "household"}):  # fmt: skip
            with self.subTest(query=query):
                before = await self.get(query)
                self.assertNotIn("habits_ready", before)
                self.assertEqual(before, await self.get({**query, "habits_only": "0"}))
        # A P3/P4 app still gets every kind together.
        self.assertEqual(
            offered(await self.get(MIXED)),
            [("room_off", "living"), ("usually_on_now", "majlis"),
             ("usually_on_now", "bedroom")],
        )  # fmt: skip


class ColdStartTest(Case):
    """Before the habits are ready: the room plans, as a fallback."""

    async def test_a_new_home_gets_the_room_plans(self):
        for query in (TABLET, PHONE):
            with self.subTest(query=query):
                self.assertEqual(
                    await self.answer(query), (False, [("room_off", "living")])
                )

    async def test_while_the_habits_warm_up(self):
        self.history(days=range(TODAY - 10, TODAY))
        self.assertEqual(await self.answer(TABLET), (False, [("room_off", "living")]))

    async def test_two_months_of_use_with_nothing_habitual(self):
        self.someone_home()
        query = self.learner.query(None)
        await query.async_learned(
            [r["room_id"] for r in p1.ROOMS], frozenset(p1.DEVICES), NOW.timestamp()
        )
        # Past the warm-up, yet no device passes the bar at any hour.
        self.assertEqual(query.summary["status"], "learning")
        self.assertEqual(await self.answer(TABLET), (False, [("room_off", "living")]))

    async def test_with_learning_off(self):
        self.history()
        await self.learner.async_update({"learning": False}, managed_ids=())
        self.assertEqual(await self.answer(TABLET), (False, [("room_off", "living")]))

    async def test_without_a_habit_learner(self):
        self.history()
        self.data.room_habits = None
        self.assertEqual(await self.answer(TABLET), (False, [("room_off", "living")]))

    async def test_the_fallback_runs_with_the_same_query(self):
        [plan] = (await self.get(TABLET))["suggestions"]
        response = await self.act(plan, "run", TABLET)
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data["status"], "succeeded")
        self.assertEqual(
            self.calls, [("light", "turn_off", {"entity_id": "light.living"})]
        )


class LearnedTest(Case):
    """Once the habits are ready: the habit kinds only, all day."""

    async def test_only_the_habit_kinds(self):
        self.history()
        self.assertEqual(
            await self.answer(TABLET),
            (True, [("usually_on_now", "majlis"), ("usually_on_now", "bedroom")]),
        )
        payload = await self.get(TABLET)
        self.assertEqual(payload["suggestion"], payload["suggestions"][0])
        # A habit is an hour's: look again at 20:00 local.
        self.assertEqual(payload["refresh_at"], "2026-10-22T17:00:00+00:00")

    async def test_for_good_not_hour_by_hour(self):
        self.history()
        # 03:10 local: nothing is usual now, and the room plans stay away.
        self.now = datetime(2026, 10, 22, 0, 10, tzinfo=UTC)
        payload = await self.get(TABLET)
        self.assertEqual(
            (payload["habits_ready"], payload["status"], payload["suggestions"]),
            (True, "no_match", []),
        )
        self.assertIsNone(payload["suggestion"])
        # A P3/P4 app at the same moment still gets the room plan.
        self.assertEqual(offered(await self.get(MIXED)), [("room_off", "living")])

    async def test_a_habit_at_another_hour_is_enough(self):
        # The only habit is the kitchen at 07:00; it is 19:10.
        self.log([p1.event(day, 7, "light.kitchen") for day in DAYS])
        self.assertEqual(await self.answer(TABLET), (True, []))

    async def test_an_off_habit_is_enough(self):
        # Only offs are habitual: the office light goes off at 23:00.
        self.someone_home()
        self.log([p1.event(day, 23, "light.office", action="off") for day in DAYS])
        self.assertEqual(await self.answer(TABLET), (True, []))

    async def test_actions_follow_the_same_rule(self):
        self.history()
        mixed = {p["kind"] + ":" + p["room_id"]: p
                 for p in (await self.get(MIXED))["suggestions"]}  # fmt: skip
        # The room plan a habits-only app no longer gets can't be acted on
        # with its query.
        response = await self.act(mixed["room_off:living"], "dismiss", TABLET)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.data["error"], "occurrence_expired_or_ineligible")
        plan = (await self.get(TABLET))["suggestions"][0]
        response = await self.act(plan, "run", TABLET)
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(
            self.calls,
            [("light", "turn_on", {"entity_id": "light.majlis"}),
             ("switch", "turn_on", {"entity_id": "switch.majlis_lamp"})],
        )  # fmt: skip

    async def test_energy_saving_leaves_the_off_habits(self):
        self.history()
        self.data.energy = mock.Mock(locked=False, active_level="medium")
        # No turn on while Energy Saving is on, and no room plans either.
        self.assertEqual(await self.answer(TABLET), (True, []))
        self.assertEqual(
            await self.answer({**TABLET, "kinds": "usually_on_now"}), (True, [])
        )


class LayerTest(Case):
    """Ready is the asker's: the tablet's on the household's habits, a
    phone's on its member's blend, the one its plans come from."""

    def history(self):
        # The majlis at the wall every evening; Alice uses the app every day,
        # for the office light, at a different hour each day.
        self.majlis_evenings()
        self.log([p1.event(day, day % 24, "light.office", "app", "alice")
                  for day in DAYS])  # fmt: skip

    async def test_a_phone_whose_blend_has_no_habit_keeps_the_fallback(self):
        self.history()
        self.assertEqual(
            await self.answer(TABLET), (True, [("usually_on_now", "majlis")])
        )
        # Alice's own layer counts 0.8 and holds no habit; the wall-switched
        # majlis is diluted under the bar on her phone.
        self.assertEqual(await self.answer(PHONE), (False, [("room_off", "living")]))
        # Bob never used the app: his phone answers from the household's.
        self.assertEqual(
            await self.answer(PHONE, sub="bob:phone"),
            (True, [("usually_on_now", "majlis")]),
        )

    async def test_a_scoped_member_is_ready_on_their_rooms(self):
        self.majlis_evenings()
        self.someone_home()
        self.assertEqual(
            await self.answer(PHONE, rooms=["majlis", "kitchen"], sub="bob:phone"),
            (True, [("usually_on_now", "majlis")]),
        )
        self.assertEqual(
            await self.answer(PHONE, rooms=["living", "kitchen"], sub="bob:phone"),
            (False, [("room_off", "living")]),
        )


class SwitchableTest(Case):
    """A habit counts only on a device a habit plan may switch."""

    async def test_an_untyped_wall_relay_turned_on_is_not_enough(self):
        # It makes the majlis on, but one tap never turns one on.
        self.someone_home()
        self.majlis_evenings(["switch.majlis_relay"])
        self.assertEqual(await self.answer(TABLET), (False, [("room_off", "living")]))

    async def test_an_untyped_wall_relay_turned_off_is(self):
        # usually_off_now turns one off.
        self.someone_home()
        self.majlis_evenings(["switch.majlis_relay"])
        self.log([p1.event(day, 22, "switch.majlis_relay", action="off")
                  for day in DAYS])  # fmt: skip
        self.assertEqual(await self.answer(TABLET), (True, []))

    async def test_a_room_out_of_room_commands_is_not_enough(self):
        self.someone_home()
        self.majlis_evenings()
        self.policies["majlis"] = {"participates": False, "eligible_entity_ids": [],
                                   "default": False}  # fmt: skip
        self.assertEqual(await self.answer(TABLET), (False, [("room_off", "living")]))
        self.policies.clear()
        self.assertEqual(
            await self.answer(TABLET), (True, [("usually_on_now", "majlis")])
        )


class AnyHabitTest(Case):
    """HabitScorer.any_habit is computed once per model, asker and devices."""

    async def scorer(self, member=None):
        query = self.learner.query(member)
        await query.async_learned(
            [r["room_id"] for r in p1.ROOMS], frozenset(p1.DEVICES), NOW.timestamp()
        )
        return query._scorer

    async def test_answers_are_kept_on_the_days_model(self):
        self.history()
        scorer = await self.scorer()
        on = {"majlis": ["light.majlis"]}
        with mock.patch.object(
            habits_module.HabitScorer, "_any_habit", autospec=True,
            side_effect=habits_module.HabitScorer._any_habit,
        ) as computed:  # fmt: skip
            self.assertTrue(scorer.any_habit(on, {}))
            self.assertTrue((await self.scorer()).any_habit(on, {}))
            self.assertEqual(computed.call_count, 1)
            # Other devices, or another asker: computed again.
            self.assertFalse(scorer.any_habit({"majlis": ["light.majlis_spot"]}, {}))
            self.assertTrue((await self.scorer("bob")).any_habit(on, {}))
            self.assertEqual(computed.call_count, 3)
        self.assertEqual(len(scorer._model.answers), 3)

    async def test_the_answers_kept_are_bounded(self):
        self.history()
        scorer = await self.scorer()
        for index in range(habits_module._MAX_ANSWERS + 5):
            scorer.any_habit({"majlis": [f"light.x{index}"]}, {})
        self.assertLessEqual(len(scorer._model.answers), habits_module._MAX_ANSWERS)

    async def test_not_before_the_warm_up(self):
        self.history(days=range(TODAY - 10, TODAY))
        query = self.learner.query(None)
        await query.async_learned(["majlis"], frozenset(p1.DEVICES), NOW.timestamp())
        self.assertIsNone(query._scorer)
        self.assertFalse(query.habits_ready({"majlis": ["light.majlis"]}, {}))


if __name__ == "__main__":
    unittest.main()
