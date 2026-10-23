"""The habit tier in the all-off ranking, and the viewer's own offs.

Runs without Home Assistant on the all-off fixture (test_room_off_ranking): the real
room_intel.ranking and off_rooms over fake states, a fake registry and a
fake activity log. The habits are a stand-in that answers like
learner.HabitQuery; the real learner is pinned in test_room_habit_learner,
the whole path over a real Home Assistant in test_room_habits_ha.
"""

import json
import unittest
from types import SimpleNamespace as NS

from test_room_off_ranking import (
    MIN,
    NOW,
    OffRankingCase,
    async_rank_rooms,
    iso,
    media,
)
from test_room_on_ranking import ac, light, relay


class Habits:
    """learner.HabitQuery's shape: the strengths it would give each room."""

    def __init__(self, strengths=None):
        self.strengths = strengths or {}
        self.asked = []

    async def async_learned(self, room_ids, counted, now_t):
        self.asked.append((list(room_ids), set(counted), now_t))

        def learned(room):
            strength = self.strengths.get(room["room_id"])
            if strength is None:
                return None
            return "habit_now", {}, (0, -strength)

        return learned


class HabitRankingCase(OffRankingCase):
    async def ranked(self, scope=None, *, member=None, now=NOW, log=True, habits=None):
        async def executor(fn, *args):
            return fn(*args)

        values = self.values
        hass = NS(
            states=NS(
                values=values,
                async_all=lambda: list(values.values()),
                get=values.get,
            ),
            rooms=self.areas,
            config=NS(time_zone="UTC"),
            async_add_executor_job=executor,
        )
        registry = NS(
            list_rooms=lambda: [dict(r) for r in self.rooms],
            list_user_devices=lambda: [dict(d) for d in self.devices],
        )
        now_data = NS(
            room_policy=lambda rid: self.policies.get(
                rid, {"participates": False, "eligible_entity_ids": []}
            )
        )
        ranked, _, _ = await async_rank_rooms(
            hass,
            registry,
            now_data,
            scope,
            activity=self.log if log else None,
            member_id=member,
            now_t=now,
            habits=habits,
        )
        return ranked

    async def order(self, scope=None, **kwargs):
        return [
            (r["room_id"], r["rank"]["reason"], r["rank"]["params"])
            for r in await self.ranked(scope, **kwargs)
        ]


class HabitTierTest(HabitRankingCase):
    async def test_usually_on_now_sits_between_just_off_and_room_order(self):
        self.home()
        self.participate_all()
        self.device("majlis", media("media_player.majlis_tv"))
        self.log.add("kitchen", "light.kitchen", "off", 5 * MIN)
        habits = Habits({"guest": 0.4, "hall": 0.9, "kitchen": 0.8, "majlis": 0.7})
        self.assertEqual(
            await self.order(habits=habits),
            [
                ("majlis", "running_load", {"loads": ["media"]}),
                ("kitchen", "just_off", {"went_dark_at": iso(NOW - 5 * MIN)}),
                # Strongest first; the tiers above win for majlis and kitchen.
                ("hall", "habit_now", {}),
                ("guest", "habit_now", {}),
                ("bedroom", "room_order", {}),
                ("office", "room_order", {}),
            ],
        )

    async def test_equal_habits_follow_the_room_order(self):
        self.home()
        self.participate_all()
        habits = Habits({"hall": 0.5, "office": 0.5, "bedroom": 0.5})
        self.assertEqual(
            [r for r, reason, _ in await self.order(habits=habits) if reason == "habit_now"],
            ["bedroom", "office", "hall"],
        )  # fmt: skip

    async def test_without_habits_it_is_the_p3_order(self):
        self.home()
        self.participate_all()
        self.log.add("kitchen", "light.kitchen", "off", 5 * MIN)
        before = await self.order()
        self.assertEqual(await self.order(habits=Habits()), before)
        self.assertNotIn("habit_now", json.dumps(before))

    async def test_habits_fill_the_empty_cards_before_the_room_order(self):
        self.home()
        self.set_on("light.majlis")
        habits = Habits({"hall": 0.6, "guest": 0.5})
        ranked = await self.ranked(habits=habits)
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["filler"]) for r in ranked],
            [
                ("majlis", "active", False),
                ("hall", "habit_now", True),
                ("guest", "habit_now", True),
                ("kitchen", "room_order", True),
                ("bedroom", "room_order", True),
            ],
        )

    async def test_rooms_that_are_on_never_rank_on_a_habit(self):
        self.home()
        self.set_on("light.office")
        self.set_on("light.bedroom")
        habits = Habits({"office": 0.9})
        on = [
            (r["room_id"], r["rank"]["reason"])
            for r in await self.ranked(habits=habits)
            if r["rank"]["active"]
        ]
        self.assertEqual(on, [("bedroom", "active"), ("office", "active")])

    async def test_the_habits_are_asked_about_the_callers_rooms_and_what_counts(self):
        self.room("majlis")
        self.room("kitchen")
        self.room("bedroom")
        self.device("majlis", light("light.majlis", "off"), ac("climate.majlis", "off"))
        self.device(
            "kitchen",
            relay("switch.kitchen_light", "off", "light"),
            relay("switch.heater", "off", "heater"),
            relay("switch.mystery", "off", None),
            relay("switch.kitchen_plug", "off", "outlet"),
            relay("switch.kitchen_relay", "off", "switch"),
        )
        self.device("kitchen", ("climate.heat_only", "heat", {"hvac_modes": ["heat"]}))
        self.device("bedroom", media("media_player.bedroom_tv", "off"))
        habits = Habits()
        await self.ranked(["kitchen", "bedroom"], habits=habits, now=NOW + 7)
        rooms, counted, now_t = habits.asked[0]
        # Only the caller's rooms; the devices that make a room on, wherever
        # they are (the model is the whole home's): lights, fans and relays
        # typed as one (what room off switches); never an AC, an outlet
        # or untyped gang, a heater gang, an unknown gang, a heat-only
        # thermostat or a TV.
        self.assertEqual(rooms, ["kitchen", "bedroom"])
        self.assertEqual(counted, {"light.majlis", "switch.kitchen_light"})
        self.assertEqual(now_t, NOW + 7)

    def set_on(self, eid):
        self.values[eid].state = "on"


class OwnOffTest(HabitRankingCase):
    """Own-tap demotion: the viewer's own off sorts after the habits."""

    async def test_the_viewers_own_off_comes_after_the_habits(self):
        self.home()
        self.participate_all()
        self.log.add("kitchen", "light.kitchen", "off", 2 * MIN, "app", "mem-1")
        self.log.add("office", "light.office", "off", 9 * MIN, "app", "mem-2")
        self.log.add("hall", "light.hall", "off", 4 * MIN, "app", "mem-1")
        habits = Habits({"guest": 0.5})
        self.assertEqual(
            await self.order(member="mem-1", habits=habits),
            [
                ("office", "just_off", {"went_dark_at": iso(NOW - 9 * MIN)}),
                ("guest", "habit_now", {}),
                # Mine, newest first.
                ("kitchen", "just_off",
                 {"went_dark_at": iso(NOW - 2 * MIN), "by_you": True}),
                ("hall", "just_off",
                 {"went_dark_at": iso(NOW - 4 * MIN), "by_you": True}),
                ("majlis", "room_order", {}),
                ("bedroom", "room_order", {}),
            ],
        )  # fmt: skip
        # For anyone else they are ordinary just_off rooms.
        self.assertEqual(
            [r for r, reason, _ in await self.order(member="mem-3", habits=habits)][:4],
            ["kitchen", "hall", "office", "guest"],
        )  # fmt: skip

    async def test_a_room_you_just_switched_off_is_never_usually_on_now(self):
        self.home()
        self.participate_all()
        self.log.add("bedroom", "light.bedroom", "off", 3 * MIN, "app", "mem-1")
        habits = Habits({"bedroom": 0.9, "office": 0.4})
        order = await self.order(member="mem-1", habits=habits)
        self.assertEqual(
            order[:2],
            [
                ("office", "habit_now", {}),
                ("bedroom", "just_off",
                 {"went_dark_at": iso(NOW - 3 * MIN), "by_you": True}),
            ],
        )  # fmt: skip

    async def test_after_thirty_minutes_your_own_off_is_gone_too(self):
        self.home()
        self.participate_all()
        self.log.add("bedroom", "light.bedroom", "off", 31 * MIN, "app", "mem-1")
        reasons = {r: reason for r, reason, _ in await self.order(member="mem-1")}
        self.assertEqual(reasons["bedroom"], "room_order")

    async def test_your_own_off_still_needs_a_real_off(self):
        # The unavailable guard applies to your own offs as well.
        self.home()
        self.participate_all()
        self.device("bedroom", light("light.bedroom_2", "off"))
        self.participate_all()
        self.log.add("bedroom", "light.bedroom", "off", 5 * MIN, "app", "mem-1")
        self.unavailable("light.bedroom_2", since_ago=60)
        reasons = {r: reason for r, reason, _ in await self.order(member="mem-1")}
        self.assertEqual(reasons["bedroom"], "room_order")


if __name__ == "__main__":
    unittest.main()
