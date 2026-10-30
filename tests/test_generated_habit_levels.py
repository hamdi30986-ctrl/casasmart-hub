"""usually_on_now at the level last used at this hour, ACs included.

The fixture of test_generated_habit_suggestions: the real suggestion
handlers, runtime, scene executor and command whitelist, the real ranking
and HabitLearner over a real SQLite room activity log. Levels are written
into the log as the recorder logs them (an on with its level, or a level
row); Home Assistant's states are fakes.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS

import test_generated_habit_suggestions as p1

TODAY, H, NOW, RIYADH = p1.TODAY, p1.H, p1.NOW, p1.RIYADH
HOUSEHOLD = {**p1.ALL_KINDS, "habits": "household"}
generated = importlib.import_module("phase4_fixture.generated_suggestions")
CommandError = importlib.import_module("phase4_fixture.entity_bridge").CommandError

# A cooling AC the way Home Assistant shows one that is off.
AC = {
    "friendly_name": "Majlis AC",
    "hvac_modes": ["off", "cool", "heat", "fan_only"],
    "fan_modes": ["low", "high", "auto"],
    "fan_mode": "auto",
    "supported_features": 1 | 8,  # target temperature, fan mode
    "min_temp": 16,
    "max_temp": 30,
    "temperature": 24,
}
COOL = {"hvac_mode": "cool", "temperature": 23, "fan_mode": "low"}
WARM = {"brightness": 204, "color_temp_kelvin": 2700}
# entity: (room, state, attributes), next to the P1 devices.
EXTRA = {
    "climate.majlis_ac": ("majlis", "off", AC),
    "fan.majlis": ("majlis", "off", {"supported_features": 1}),
    "climate.study_ac": ("study", "off", {**AC, "friendly_name": "Study AC"}),
}


def at(day, hour, minute=5):
    return day * 86400 + hour * 3600 + minute * 60 - RIYADH * 60


def use(day, hour, entity, level=None, *, action="on", minute=5, **extra):
    """A row as the recorder logs it: the device's room, its level."""
    room = (EXTRA.get(entity) or p1.DEVICES[entity])[0]
    return {
        "t": at(day, hour, minute),
        "utc_offset_min": RIYADH,
        "room_id": room,
        "entity_id": entity,
        "action": action,
        "source": extra.get("source", "wall_switch"),
        "member_id": extra.get("member"),
        "level": level,
    }


class LevelCase(p1.HabitSuggestionCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for entity_id, (room, state, attributes) in EXTRA.items():
            self.states.values[entity_id] = NS(
                entity_id=entity_id, state=state, attributes=dict(attributes)
            )
            self.hass.rooms[entity_id] = room
        self.registry.list_rooms = lambda: [
            *(dict(r) for r in p1.ROOMS),
            {"room_id": "study", "name": "Study", "sort_order": 5},
        ]
        self.registry.list_user_devices = lambda: [
            {"control_entity_ids": list(self.states.values), "gangs": p1.GANGS}
        ]
        self.attrs("light.majlis").update(
            supported_color_modes=["color_temp"],
            min_color_temp_kelvin=2000,
            max_color_temp_kelvin=6500,
        )
        self.log = self.db.room_activity()

    def attrs(self, entity_id):
        return self.states.values[entity_id].attributes

    def every_day(self, entity, *, days=range(TODAY - 56, TODAY), **extra):
        """entity switched on at H every day, with no level (a v5 row)."""
        self.log.append_many(
            [{**use(day, H, entity, **extra), "level": None} for day in days]
        )

    async def levels(self, room, ids, now=None):
        now = now or NOW
        return await self.learner.async_levels(room, ids, now.timestamp())

    async def plans(self, query=HOUSEHOLD):
        return {
            p["room_id"]: p["actions"]
            for p in (await self.get(query))["suggestions"]
            if p["kind"] == "usually_on_now"
        }

    async def plan(self, room="majlis", query=HOUSEHOLD):
        return next(
            p
            for p in (await self.get(query))["suggestions"]
            if p["kind"] == "usually_on_now" and p["room_id"] == room
        )


def steps(actions):
    return [(a["entity_id"], a["action"], a["data"]) for a in actions]


class LastUsedLevelTest(LevelCase):
    async def test_the_newest_level_at_this_hour_wins(self):
        self.log.append_many(
            [
                use(TODAY - 8, H, "light.majlis", {"brightness": 50}),
                use(TODAY - 1, H, "light.majlis", {"brightness": 220}),
                # The same evening, later: not this hour's.
                use(TODAY - 1, 22, "light.majlis", {"brightness": 10}),
            ]
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 220}},
        )
        # Dimmed later in that hour: the level it was last used at.
        self.log.append_many(
            [use(TODAY - 1, H, "light.majlis", {"brightness": 180}, action="level",
                 minute=40)]
        )  # fmt: skip
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 180}},
        )
        # Today's use counts at once.
        self.log.append_many(
            [use(TODAY, H, "light.majlis", {"brightness": 99}, minute=2)]
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis", "light.majlis_spot"]),
            {"light.majlis": {"brightness": 99}},
        )

    async def test_an_hour_either_side_when_there_is_none_at_this_hour(self):
        self.log.append_many(
            [
                use(TODAY - 2, H + 1, "light.majlis", {"brightness": 30}),
                use(TODAY - 1, H - 1, "light.majlis", {"brightness": 70}, minute=55),
            ]
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 70}},
        )
        # One at this hour wins, older as it is (last Thursday).
        self.log.append_many([use(TODAY - 7, H, "light.majlis", {"brightness": 5})])
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 5}},
        )

    async def test_a_day_like_today(self):
        # Thursday is a workday; last Saturday's movie night isn't its level.
        self.log.append_many(
            [
                use(TODAY - 7, H, "light.majlis", {"brightness": 200}),
                use(TODAY - 5, H, "light.majlis", {"brightness": 15}),
            ]
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 200}},
        )
        friday = NOW + timedelta(days=1)
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"], friday),
            {"light.majlis": {"brightness": 15}},
        )

    async def test_only_what_the_habits_may_learn_from(self):
        self.log.append_many(
            [
                use(TODAY - 3, H, "light.majlis", {"brightness": 40}, source="app",
                    member="alice"),
                use(TODAY - 2, H, "light.majlis", {"brightness": 90}),
                use(TODAY - 1, H, "light.majlis", {"brightness": 1},
                    source="automation"),
                use(TODAY - 1, H, "light.majlis", {"brightness": 2}, source="unknown"),
            ]
        )  # fmt: skip
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 90}},
        )
        # A device on its own schedule: its wall switch rows don't count.
        await self.learner.async_update(
            {"self_scheduled_entity_ids": ["light.majlis"]},
            managed_ids={"light.majlis"},
            now_t=NOW.timestamp(),
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 40}},
        )
        # After a reset nothing before it counts, today included.
        await self.learner.async_reset(now_t=at(TODAY - 3, 12))
        self.assertEqual(await self.levels("majlis", ["light.majlis"]), {})
        self.log.append_many(
            [use(TODAY - 2, H, "light.majlis", {"brightness": 33}, source="app")]
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 33}},
        )
        # Paused since this morning: today's use isn't learned.
        await self.learner.async_update({"paused": True}, managed_ids=(),
                                        now_t=at(TODAY, 8))  # fmt: skip
        self.log.append_many(
            [use(TODAY, H, "light.majlis", {"brightness": 77}, source="app", minute=1)]
        )
        self.assertEqual(
            await self.levels("majlis", ["light.majlis"]),
            {"light.majlis": {"brightness": 33}},
        )
        await self.learner.async_update({"learning": False}, managed_ids=())
        self.assertEqual(await self.levels("majlis", ["light.majlis"]), {})

    async def test_no_habits_no_levels_and_a_failed_read_none(self):
        self.log.append_many([use(TODAY - 1, H, "light.majlis", {"brightness": 9})])
        query = self.learner.query(None)
        # Asked before the habits answered, or while they warm up: none.
        self.assertEqual(await query.async_levels("majlis", ["light.majlis"]), {})
        await query.async_learned(["majlis"], frozenset(p1.DEVICES), NOW.timestamp())
        self.assertEqual(query.summary["status"], "warming_up")
        self.assertEqual(await query.async_levels("majlis", ["light.majlis"]), {})

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        self.learner._activity = NS(level_rows=broken)
        with self.assertLogs(p1.learner_module.__name__, "WARNING") as logs:
            self.assertEqual(await self.levels("majlis", ["light.majlis"]), {})
            self.assertEqual(await self.levels("majlis", ["light.majlis"]), {})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("levels could not be read", logs.output[0])


class LightLevelPlanTest(LevelCase):
    async def test_turn_on_restores_the_level_last_used_at_this_hour(self):
        self.history()
        self.log.append_many(
            [
                use(TODAY - 2, H, "light.majlis", {"brightness": 90}),
                use(TODAY - 1, H, "light.majlis", WARM),
                use(TODAY - 1, H, "light.bedroom_lamp", {"brightness": 30},
                    source="app", member="alice"),
            ]
        )  # fmt: skip
        self.attrs("light.bedroom_lamp")["supported_color_modes"] = ["brightness"]
        plans = await self.plans()
        self.assertEqual(
            steps(plans["majlis"]),
            [
                ("light.majlis", "turn_on", WARM),
                # A wall relay has no level: plain on, as before.
                ("switch.majlis_lamp", "turn_on", {}),
            ],
        )
        self.assertEqual(
            steps(plans["bedroom"]),
            [
                ("light.bedroom_lamp", "turn_on", {"brightness": 30}),
                ("light.bedroom_main", "turn_on", {}),  # nothing recorded
            ],
        )

    async def test_only_the_level_the_device_still_takes(self):
        self.history()
        self.log.append_many(
            [
                use(TODAY - 1, H, "light.majlis", WARM),
                use(TODAY - 1, H, "light.bedroom_lamp", {"brightness": 30}),
            ]
        )
        # Warm white is past its range now; the bedroom lamp is on/off only.
        self.attrs("light.majlis")["max_color_temp_kelvin"] = 2500
        self.attrs("light.bedroom_lamp")["supported_color_modes"] = ["onoff"]
        plans = await self.plans()
        self.assertEqual(plans["majlis"][0]["data"], {"brightness": 204})
        self.assertEqual(plans["bedroom"][0]["data"], {})
        self.attrs("light.majlis")["supported_color_modes"] = ["brightness"]
        self.assertEqual((await self.plans())["majlis"][0]["data"], {"brightness": 204})

    async def test_a_fan_comes_on_at_its_last_speed(self):
        self.history()
        self.every_day("fan.majlis")
        self.log.append_many([use(TODAY - 1, H, "fan.majlis", {"percentage": 66})])
        self.assertIn(
            ("fan.majlis", "turn_on", {"percentage": 66}),
            steps((await self.plans())["majlis"]),
        )
        self.attrs("fan.majlis")["supported_features"] = 0  # no speeds
        self.assertIn(
            ("fan.majlis", "turn_on", {}), steps((await self.plans())["majlis"])
        )

    async def test_the_occurrence_follows_the_level(self):
        self.history()
        self.log.append_many([use(TODAY - 1, H, "light.majlis", {"brightness": 90})])
        shown = await self.plan()
        self.log.append_many(
            [use(TODAY, H, "light.majlis", {"brightness": 140}, minute=1)]
        )
        changed = await self.plan()
        self.assertEqual(changed["suppression_id"], shown["suppression_id"])
        self.assertNotEqual(changed["occurrence_id"], shown["occurrence_id"])
        # What was shown can't run as something else.
        response = await self.act(shown, query=HOUSEHOLD)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.calls, [])


class AcPlanTest(LevelCase):
    async def test_an_ac_comes_on_at_its_last_mode_target_and_fan(self):
        self.history()
        self.every_day("climate.majlis_ac")
        self.log.append_many(
            [
                use(TODAY - 2, H, "climate.majlis_ac", {"hvac_mode": "heat",
                    "temperature": 26}),
                use(TODAY - 1, H, "climate.majlis_ac", COOL),
            ]
        )  # fmt: skip
        plan = await self.plan()
        self.assertEqual(
            plan["actions"][:3],
            [
                {"entity_id": "climate.majlis_ac", "action": "set_hvac_mode",
                 "data": {"hvac_mode": "cool"}, "name": "Majlis AC"},
                {"entity_id": "climate.majlis_ac", "action": "set_temperature",
                 "data": {"temperature": 23.0}, "name": "Majlis AC"},
                {"entity_id": "climate.majlis_ac", "action": "set_fan_mode",
                 "data": {"fan_mode": "low"}, "name": "Majlis AC"},
            ],
        )  # fmt: skip
        self.assertEqual(
            [a["entity_id"] for a in plan["actions"][3:]],
            ["light.majlis", "switch.majlis_lamp"],
        )
        # A fan mode it no longer has is left out; the rest still go.
        self.attrs("climate.majlis_ac")["fan_modes"] = ["auto"]
        self.assertEqual(
            [a["action"] for a in (await self.plan())["actions"][:3]],
            ["set_hvac_mode", "set_temperature", "turn_on"],
        )

    async def test_no_ac_without_a_recorded_mode_and_target(self):
        self.history()
        self.every_day("climate.majlis_ac")
        # Every on is from before levels were recorded.
        self.assertEqual(
            [e for e, _, _ in steps((await self.plans())["majlis"])],
            ["light.majlis", "switch.majlis_lamp"],
        )
        # Last used in fan only: a mode, no target.
        self.log.append_many(
            [use(TODAY - 1, H, "climate.majlis_ac", {"hvac_mode": "fan_only"})]
        )
        self.assertNotIn(
            "climate.majlis_ac",
            [e for e, _, _ in steps((await self.plans())["majlis"])],
        )

    async def test_no_ac_when_it_cant_take_that_level_now(self):
        self.history()
        self.every_day("climate.majlis_ac")
        self.log.append_many([use(TODAY - 1, H, "climate.majlis_ac", COOL)])
        for change in (
            {"hvac_modes": ["off", "heat", "fan_only"]},  # no cooling: no AC
            {"hvac_modes": ["off", "cool"], "min_temp": 24},  # 23 below its range
            {"max_temp": 22},
            {"supported_features": 8},  # no target temperature
            {"casasmart_room_activity_exclude": True},
        ):
            with self.subTest(change=change):
                self.states.values["climate.majlis_ac"].attributes = {**AC, **change}
                self.assertNotIn(
                    "climate.majlis_ac",
                    [e for e, _, _ in steps((await self.plans())["majlis"])],
                )
        # Unavailable, or already running: nothing to turn on.
        self.states.values["climate.majlis_ac"].attributes = dict(AC)
        self.states.values["climate.majlis_ac"].state = "unavailable"
        self.assertNotIn(
            "climate.majlis_ac",
            [e for e, _, _ in steps((await self.plans())["majlis"])],
        )

    async def test_a_room_whose_only_usual_device_is_an_ac(self):
        self.history()
        self.every_day("climate.study_ac")
        # The majlis and the bedroom are on, so the next rooms come in.
        self.states.values["light.majlis_spot"].state = "on"
        self.states.values["light.living"].state = "off"
        self.states.values["light.bedroom_main"].state = "on"
        self.assertEqual(list(await self.plans()), ["office"])
        self.log.append_many([use(TODAY - 1, H, "climate.study_ac", COOL)])
        plans = await self.plans()
        self.assertEqual(list(plans), ["study", "office"])
        self.assertEqual(
            [a["action"] for a in plans["study"]],
            ["set_hvac_mode", "set_temperature", "set_fan_mode"],
        )


class AcRunTest(LevelCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.history()
        self.every_day("climate.majlis_ac")
        self.log.append_many(
            [
                use(TODAY - 1, H, "climate.majlis_ac", COOL),
                use(TODAY - 1, H, "light.majlis", WARM),
            ]
        )
        original = self.hass.services.async_call

        async def call(domain, service, data, blocking=True, context=None):
            await original(domain, service, data, blocking=blocking, context=context)
            if service == "set_hvac_mode":
                self.states.values[data["entity_id"]].state = self.mode_after

        self.mode_after = "cool"
        self.hass.services.async_call = call

    async def test_run_sends_the_mode_then_the_target_not_the_fan(self):
        plan = await self.plan()
        # The plan shows the fan mode last used...
        self.assertIn(
            ("climate.majlis_ac", "set_fan_mode", {"fan_mode": "low"}),
            steps(plan["actions"]),
        )
        response = await self.act(plan, query=HOUSEHOLD)
        self.assertEqual(response.data["status"], "succeeded", response.data)
        # ...but the run skips it after the mode, as a saved scene does: an
        # IR AC takes each call as a full-state burst and garbles a third.
        self.assertEqual(
            self.calls,
            [
                ("climate", "set_hvac_mode",
                 {"entity_id": "climate.majlis_ac", "hvac_mode": "cool"}),
                ("climate", "set_temperature",
                 {"entity_id": "climate.majlis_ac", "temperature": 23.0}),
                ("light", "turn_on",
                 {"entity_id": "light.majlis", **WARM}),
                ("switch", "turn_on", {"entity_id": "switch.majlis_lamp"}),
            ],
        )  # fmt: skip
        self.assertEqual(
            (response.data["succeeded_count"], response.data["failed_count"]), (4, 0)
        )

    async def test_an_ac_that_reports_its_mode_late_still_gets_its_target(self):
        # A cloud AC: still "off" in Home Assistant right after the mode step.
        self.mode_after = "off"
        response = await self.act(await self.plan(), query=HOUSEHOLD)
        self.assertEqual(response.data["status"], "succeeded")
        self.assertEqual(
            [s for _, s, _ in self.calls[:3]],
            ["set_hvac_mode", "set_temperature", "turn_on"],
        )

    async def test_an_ac_someone_set_meanwhile_keeps_their_setting(self):
        plan = await self.plan()
        # Right after the mode step, someone sets it to heat by hand.
        self.mode_after = "heat"
        response = await self.act(plan, query=HOUSEHOLD)
        self.assertEqual(response.data["status"], "partial_failure")
        self.assertEqual(
            [(d, s) for d, s, _ in self.calls],
            [("climate", "set_hvac_mode"), ("light", "turn_on"), ("switch", "turn_on")],
        )
        # Its target step was skipped, not sent (and the fan step never is).
        self.assertEqual(
            (response.data["succeeded_count"], response.data["failed_count"]), (3, 1)
        )

    async def test_energy_saving_still_hides_and_refuses_it(self):
        plan = await self.plan()
        self.data.energy = NS(locked=False, active_level="medium")
        self.assertEqual((await self.act(plan, query=HOUSEHOLD)).status, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(await self.plans(), {})


class IrAcRuleTest(LevelCase):
    """One rule for an AC's fan step after its mode (registry_api's
    async_execute_registry_scene): a saved scene and a usually_on_now plan
    skip it, room_eco sends it."""

    async def run_scene(self, entities, **scene):
        # The executor the suggestion views run (the fixture's).
        return await p1.API.async_execute_registry_scene(
            self.hass, {"scene_id": "s", "entities": entities, **scene}
        )

    async def test_a_saved_scene_skips_the_fan_after_the_mode_or_target(self):
        ac = "climate.majlis_ac"
        mode = {"entity_id": ac, "action": "set_hvac_mode",
                "data": {"hvac_mode": "cool"}}  # fmt: skip
        target = {"entity_id": ac, "action": "set_temperature",
                  "data": {"temperature": 23}}  # fmt: skip
        fan = {"entity_id": ac, "action": "set_fan_mode", "data": {"fan_mode": "low"}}
        for entities, sent in (
            ([mode, target, fan], ["set_hvac_mode", "set_temperature"]),
            ([target, fan], ["set_temperature"]),
            ([fan], ["set_fan_mode"]),
        ):
            with self.subTest(sent=sent):
                self.calls.clear()
                result = await self.run_scene(entities)
                self.assertEqual([s for _, s, _ in self.calls], sent)
                self.assertEqual(len(result["results"]), len(sent))
                self.assertTrue(result["ok"])

    async def test_room_eco_still_sends_the_target_and_the_fan(self):
        self.states.values["light.majlis"].state = "on"
        self.states.values["light.majlis_spot"].state = "on"
        ac = self.states.values["climate.majlis_ac"]
        ac.state = "cool"
        ac.attributes.update(temperature=22, fan_mode="auto")
        query = {"kinds": "room_eco"}
        [plan] = (await self.get(query))["suggestions"]
        self.assertEqual(
            [s for e, s, _ in steps(plan["actions"]) if e == "climate.majlis_ac"],
            ["set_temperature", "set_fan_mode"],
        )
        response = await self.act(plan, query=query)
        self.assertEqual(response.data["status"], "succeeded", response.data)
        self.assertEqual(
            [s for d, s, _ in self.calls if d == "climate"],
            ["set_temperature", "set_fan_mode"],
        )


class WhitelistTest(unittest.TestCase):
    """What a usually_on_now step may be (generated_suggestions), and the
    command whitelist it runs through (entity_bridge), unchanged."""

    def state(self, entity_id, state="off", **attributes):
        return NS(entity_id=entity_id, state=state, attributes=attributes)

    def test_usual_steps(self):
        applies = generated.usual_step_applies
        light, relay, fan = (
            self.state("light.a"),
            self.state("switch.a"),
            self.state("fan.a"),
        )
        ac = self.state("climate.a", **AC)

        def step(entity, action, **data):
            return {"entity_id": entity, "action": action, "data": data}

        mode = step("climate.a", "set_hvac_mode", hvac_mode="cool")
        plan = [mode, step("climate.a", "set_temperature", temperature=23)]
        yes = [
            (light, step("light.a", "turn_on")),
            (light, step("light.a", "turn_on", brightness=9, color_temp_kelvin=2700)),
            (fan, step("fan.a", "turn_on", percentage=50)),
            (relay, step("switch.a", "turn_on")),
            (ac, mode),
            (ac, plan[1]),
            (ac, step("climate.a", "set_fan_mode", fan_mode="low")),
        ]
        for state, item in yes:
            with self.subTest(item=item):
                self.assertTrue(applies(state, item, plan))
        no = [
            (light, step("light.a", "turn_on", rgb_color=[1, 2, 3])),
            (light, step("light.a", "turn_on", effect="party")),
            (light, step("light.a", "toggle")),
            (relay, step("switch.a", "turn_on", brightness=9)),
            (fan, step("fan.a", "turn_on", preset_mode="boost")),
            (self.state("light.a", "on"), step("light.a", "turn_on")),
            (ac, step("climate.a", "turn_on")),
            (ac, step("climate.a", "set_temperature", temperature=23, hvac_mode="heat")),
            (ac, step("climate.a", "set_preset_mode", preset_mode="eco")),
            (ac, step("climate.a", "set_hvac_mode", hvac_mode="cool", temperature=2)),
            (self.state("climate.a", "heat", **AC), mode),
            (self.state("climate.a", "heat", **AC), plan[1]),
            (self.state("climate.a", **{**AC, "hvac_modes": ["off", "heat"]}), mode),
            (self.state("climate.a", **{**AC, "casasmart_room_activity_exclude": True}),
             mode),
            (self.state("cover.a"), step("cover.a", "open")),
            (self.state("lock.a"), step("lock.a", "unlock")),
        ]  # fmt: skip
        for state, item in no:
            with self.subTest(item=item):
                self.assertFalse(applies(state, item, plan))
        # A target or fan step needs the plan's own mode step for that AC.
        self.assertFalse(applies(ac, plan[1], [plan[1]]))
        self.assertTrue(applies(self.state("climate.a", "cool", **AC), plan[1], plan))

    def test_the_command_whitelist_is_unchanged(self):
        bridge = importlib.import_module("phase4_fixture.entity_bridge")
        with self.assertRaises(CommandError):
            bridge.validate_command("climate.a", "turn_on", {})
        self.assertEqual(
            bridge.validate_command(
                "climate.a", "set_hvac_mode", {"hvac_mode": "cool"}
            ),
            ("climate", "set_hvac_mode", {"hvac_mode": "cool"}),
        )


class CompatibilityTest(LevelCase):
    async def test_without_kinds_levels_and_acs_change_nothing(self):
        self.history()
        self.every_day("climate.majlis_ac")
        self.log.append_many(
            [
                use(TODAY - 1, H, "climate.majlis_ac", COOL),
                use(TODAY - 1, H, "light.majlis", WARM),
            ]
        )
        # A running AC in the active room, as the old kinds see it.
        self.states.values["climate.study_ac"].state = "cool"
        self.states.values["light.living"].attributes.update(
            supported_color_modes=["brightness"], brightness=255
        )
        old_app = await self.get()
        self.data.room_habits = None
        self.assertEqual(old_app, await self.get())
        self.assertEqual(old_app, await self.get({"kinds": "room_off,room_eco"}))
        text = json.dumps(old_app)
        for absent in ("usually_on_now", "set_hvac_mode", "color_temp_kelvin"):
            self.assertNotIn(absent, text)


if __name__ == "__main__":
    unittest.main()
