"""usually_off_now: generated "turn off" plans from the room habits.

The fixture of test_generated_habit_levels (and P1's before it): the real
suggestion handlers, runtime, scene executor and command whitelist, the
real ranking and HabitLearner over a real SQLite room activity log. History
is written into the log as the recorder logs it; Home Assistant's states
are fakes.
"""

from __future__ import annotations

import importlib
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest import mock

import test_generated_habit_levels as p2
import test_generated_habit_suggestions as p1

TODAY, H, NOW, RIYADH = p1.TODAY, p1.H, p1.NOW, p1.RIYADH
generated = importlib.import_module("phase4_fixture.generated_suggestions")
learner_module = importlib.import_module("phase4_fixture.room_intel.learner")
capabilities = importlib.import_module("phase4_fixture.capabilities")

OFF = {"kinds": "usually_off_now", "habits": "household"}
EVERY = {"kinds": "room_off,room_eco,usually_on_now,usually_off_now"}
HOUSEHOLD = {**EVERY, "habits": "household"}
COOLING = {**p2.AC, "hvac_modes": ["off", "cool", "heat", "fan_only"]}
# entity: (room, state, attributes), next to the P1 and P2 devices: what
# room off never switches.
PROTECTED = {
    "switch.majlis_heater": ("majlis", "on", {}),
    "switch.kitchen_fridge": ("kitchen", "on", {}),
    "lock.majlis_door": ("majlis", "unlocked", {}),
    "cover.majlis_shutter": ("majlis", "open", {}),
    "media_player.majlis_tv": ("majlis", "on", {}),
    "light.majlis_night": ("majlis", "on", {"casasmart_room_activity_exclude": True}),
    "climate.majlis_heater": ("majlis", "heat", {"hvac_modes": ["off", "heat"]}),
}
GANGS = {
    **p1.GANGS,
    "switch.majlis_heater": {"type": "heater"},
    "switch.kitchen_fridge": {"type": "outlet"},
}
# The majlis' devices room off switches (and its AC), on this evening.
MAJLIS_ON = [
    "climate.majlis_ac",
    "fan.majlis",
    "light.majlis",
    "switch.majlis_lamp",
    "switch.majlis_relay",
]


def room_of(entity):
    return (PROTECTED.get(entity) or p2.EXTRA.get(entity) or p1.DEVICES[entity])[0]


def row(day, hour, entity, action="on", source="wall_switch", member=None):
    return {
        "t": p2.at(day, hour),
        "utc_offset_min": RIYADH,
        "room_id": room_of(entity),
        "entity_id": entity,
        "action": action,
        "source": source,
        "member_id": member,
    }


def turn_off(entity, name=None):
    return {
        "entity_id": entity,
        "action": "turn_off",
        "data": {},
        "name": name or entity,
    }


class OffCase(p2.LevelCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for entity_id, (room, state, attributes) in PROTECTED.items():
            self.states.values[entity_id] = NS(
                entity_id=entity_id, state=state, attributes=dict(attributes)
            )
            self.hass.rooms[entity_id] = room
        self.registry.list_user_devices = lambda: [
            {"control_entity_ids": list(self.states.values), "gangs": GANGS}
        ]
        self.attrs("climate.majlis_ac").update(hvac_modes=COOLING["hvac_modes"])
        # This evening: the majlis is on, AC cooling, as are the protected
        # devices.
        for entity_id in MAJLIS_ON:
            self.states.values[entity_id].state = "on"
        self.states.values["climate.majlis_ac"].state = "cool"
        self.states.values["switch.majlis_plug"].state = "on"

    def evenings(self, entities, *, hour=H, source="wall_switch", member=None,
                 days=range(TODAY - 56, TODAY)):  # fmt: skip
        """Every day: these on at 16:05 and off at hour:05, the kitchen at
        07:05 (someone is home)."""
        rows = []
        for day in days:
            rows.append(row(day, 7, "light.kitchen"))
            for entity in entities:
                rows.append(row(day, 16, entity, "on", source, member))
                rows.append(row(day, hour, entity, "off", source, member))
        self.log.append_many(rows)

    def history(self):
        """The majlis switched off at 19:05 every evening, at the wall, with
        everything else in it (and the kitchen fridge) as well."""
        self.evenings(MAJLIS_ON + ["switch.majlis_plug", *PROTECTED])

    async def offs(self, query=OFF, **claims):
        return [
            p for p in (await self.get(query, **claims))["suggestions"]
            if p["kind"] == "usually_off_now"
        ]  # fmt: skip

    async def off_rooms(self, query=OFF, **claims):
        return [
            (p["room_id"], [a["entity_id"] for a in p["actions"]])
            for p in await self.offs(query, **claims)
        ]


class CapabilityGateTest(OffCase):
    async def test_only_a_client_that_asks_gets_usually_off_now(self):
        self.history()
        for query in ({}, {"kinds": "room_off,room_eco"},
                      {"kinds": "room_off,room_eco,usually_on_now",
                       "habits": "household"}):  # fmt: skip
            with self.subTest(query=query):
                payload = await self.get(query)
                self.assertNotIn("usually_off_now", json.dumps(payload))
        self.assertEqual(
            [(p["kind"], p["room_id"]) for p in (await self.get(HOUSEHOLD))["suggestions"]],
            [("room_off", "majlis"), ("room_eco", "majlis"), ("room_off", "living"),
             ("usually_off_now", "majlis")],
        )  # fmt: skip
        self.assertEqual(
            generated.ask_from_query(OFF),
            generated.Ask(frozenset({"usually_off_now"}), household=True),
        )

    async def test_the_handshake_advertises_it(self):
        feature = capabilities.handshake_capabilities()["features"][
            "generated_room_habit_off_suggestions_v1"
        ]
        self.assertEqual(
            feature, {"version": 1, "minimum_api_version": 1, "available": True}
        )


class PlanTest(OffCase):
    async def test_the_devices_usually_off_by_now_that_are_on(self):
        self.history()
        payload = await self.get(OFF)
        [plan] = payload["suggestions"]
        self.assertEqual(
            {k: plan[k] for k in ("source", "kind", "room_id", "room_name")},
            {"source": "generated_room_v1", "kind": "usually_off_now",
             "room_id": "majlis", "room_name": "Majlis"},
        )  # fmt: skip
        self.assertEqual(
            plan["scene"],
            {"scene_id": plan["occurrence_id"], "name": "Turn off · Majlis",
             "icon": "power"},
        )  # fmt: skip
        # Every one with the same habit: by entity id. Never the outlet, the
        # heater relay, the fridge, the lock, the shutter, the TV, a device
        # left out of room activity or a heating-only thermostat.
        self.assertEqual(
            plan["actions"],
            [
                turn_off("climate.majlis_ac", "Majlis AC"),
                turn_off("fan.majlis"),
                turn_off("light.majlis", "Majlis light"),
                turn_off("switch.majlis_lamp"),
                turn_off("switch.majlis_relay"),
            ],
        )
        self.assertEqual(
            plan["reason"], {"code": "usually_off_now", "layer": "household", "hour": H}
        )
        self.assertEqual(plan["expires_at"], "2026-10-22T18:00:00+00:00")
        self.assertEqual(plan["suppression_id"], plan["rule_id"])
        self.assertEqual(len(plan["occurrence_id"]), 64)
        # A habit is an hour's: look again at 20:00 local.
        self.assertEqual(payload["refresh_at"], "2026-10-22T17:00:00+00:00")
        self.assertEqual(self.calls, [])

    async def test_the_protected_devices_stay_out_even_if_habits_said_so(self):
        self.history()
        everything = [*MAJLIS_ON, "switch.majlis_plug", *PROTECTED]
        with mock.patch.object(
            learner_module.HabitQuery,
            "usual_off_devices",
            lambda query, room_id: list(everything),
        ):
            self.assertEqual(await self.off_rooms(), [("majlis", MAJLIS_ON)])

    async def test_only_what_is_on_and_usually_off(self):
        self.history()
        # The spot is on but has no habit; the light went off already.
        self.states.values["light.majlis_spot"].state = "on"
        self.states.values["light.majlis"].state = "off"
        rooms = await self.off_rooms()
        self.assertEqual(
            rooms, [("majlis", [e for e in MAJLIS_ON if e != "light.majlis"])]
        )
        # An AC in a mode it has no "off" for can't be switched off.
        self.attrs("climate.majlis_ac")["hvac_modes"] = ["cool", "fan_only"]
        self.assertNotIn("climate.majlis_ac", (await self.off_rooms())[0][1])
        # Nothing on, nothing to offer.
        for entity_id in MAJLIS_ON:
            self.states.values[entity_id].state = "off"
        self.assertEqual(await self.off_rooms(), [])

    async def test_from_the_usual_hour_for_two_hours(self):
        self.history()
        for hours, expected in ((-2, False), (-1, False), (0, True), (1, True),
                                (2, False)):  # fmt: skip
            with self.subTest(hours=hours):
                self.now = NOW + timedelta(hours=hours)
                self.assertEqual(bool(await self.off_rooms()), expected)

    async def test_a_room_out_of_room_commands_and_a_policys_own_devices(self):
        self.history()
        self.policies["majlis"] = {"participates": False,
                                   "eligible_entity_ids": [], "default": False}  # fmt: skip
        self.assertEqual(await self.off_rooms(), [])
        # A stored policy switches only the devices on its list, and the ACs.
        self.policies["majlis"] = {"participates": True,
                                   "eligible_entity_ids": ["light.majlis"],
                                   "default": False}  # fmt: skip
        self.assertEqual(
            await self.off_rooms(), [("majlis", ["climate.majlis_ac", "light.majlis"])]
        )

    async def test_at_most_two_rooms_busiest_first(self):
        self.history()
        self.evenings(["light.bedroom_main", "light.office"])
        for entity_id in ("light.bedroom_main", "light.office"):
            self.states.values[entity_id].state = "on"
        # The living room is on too, with no habit.
        self.assertEqual(
            await self.off_rooms(),
            [("majlis", MAJLIS_ON), ("bedroom", ["light.bedroom_main"])],
        )

    async def test_strongest_first_within_a_room(self):
        self.evenings(["light.majlis"])
        self.evenings(["fan.majlis"], days=range(TODAY - 56, TODAY, 2))
        self.assertEqual(
            await self.off_rooms(), [("majlis", ["light.majlis", "fan.majlis"])]
        )

    async def test_a_device_just_switched_on_is_in_use(self):
        self.history()
        # The light came on at the wall 10 minutes ago, the fan 25.
        now = NOW.timestamp()
        self.states.values["light.majlis"].last_changed_timestamp = now - 600
        self.states.values["fan.majlis"].last_changed_timestamp = now - 1500
        payload = await self.get(OFF)
        self.assertEqual(
            [a["entity_id"] for a in payload["suggestions"][0]["actions"]],
            ["climate.majlis_ac", "switch.majlis_lamp", "switch.majlis_relay"],
        )
        # Look again when the fan has been on 30 minutes (19:15 local).
        self.assertEqual(payload["refresh_at"], "2026-10-22T16:15:00+00:00")
        self.now = NOW + timedelta(minutes=5)
        self.assertIn("fan.majlis", (await self.off_rooms())[0][1])
        self.now = NOW + timedelta(minutes=20)
        self.assertEqual(await self.off_rooms(), [("majlis", MAJLIS_ON)])
        # Changed in the future (a clock gone back): still held back.
        self.states.values["fan.majlis"].last_changed_timestamp = now + 7200
        self.assertNotIn("fan.majlis", (await self.off_rooms())[0][1])


class LayerTest(OffCase):
    async def test_the_tablet_gets_the_households_and_a_phone_its_members(self):
        # Alice switches the AC on and off from her phone; the rest goes
        # off at the wall.
        self.evenings(["climate.majlis_ac"], source="app", member="alice")
        self.evenings([e for e in MAJLIS_ON if e != "climate.majlis_ac"])
        self.assertEqual(await self.off_rooms(OFF), [("majlis", MAJLIS_ON)])
        mine = await self.offs({"kinds": "usually_off_now"})
        self.assertEqual(
            [(p["room_id"], [a["entity_id"] for a in p["actions"]],
              p["reason"]["layer"]) for p in mine],
            [("majlis", ["climate.majlis_ac"], "personal")],
        )  # fmt: skip
        # Bob, with no app history of his own, gets the household's.
        self.assertEqual(
            await self.off_rooms({"kinds": "usually_off_now"}, sub="bob:phone"),
            [("majlis", MAJLIS_ON)],
        )

    async def test_no_habits_no_turn_off(self):
        self.history()
        self.data.room_habits = None
        self.assertEqual(await self.off_rooms(), [])
        payload = await self.get(OFF)
        self.assertEqual(payload["refresh_at"], "2026-10-22T18:00:00+00:00")
        # Two weeks of history are needed first.
        self.data.room_habits = self.learner
        await self.learner.async_reset(NOW.timestamp() - 10 * 86400)
        self.assertEqual(await self.off_rooms(), [])


class EnergySavingTest(OffCase):
    async def test_turning_off_is_offered_and_runs_during_energy_saving(self):
        self.history()
        self.states.values["light.majlis_spot"].state = "off"
        self.every_day("light.majlis_spot")  # usually on at 19:00, but off
        self.data.energy = NS(locked=False, active_level="medium")
        payload = await self.get(HOUSEHOLD)
        self.assertEqual(
            [p["kind"] for p in payload["suggestions"]],
            ["room_off", "room_eco", "room_off", "usually_off_now"],
        )
        context = await self.runtime.generated.context(
            None, generated.Ask(generated.KINDS, household=True)
        )
        self.assertEqual(
            {s["kind"]: s["works_during_energy_saving"] for s in context[1].values()},
            {"room_off": True, "room_eco": True, "usually_off_now": True},
        )
        plan = payload["suggestions"][-1]
        response = await self.act(plan, query=HOUSEHOLD)
        self.assertEqual(response.data["status"], "succeeded", response.data)
        self.assertEqual(len(self.calls), 5)
        # Without Energy Saving the majlis spot's turn on is back too.
        self.data.energy = None
        self.assertIn(
            "usually_on_now", [p["kind"] for p in (await self.get(HOUSEHOLD))["suggestions"]]
        )  # fmt: skip


class RunTest(OffCase):
    async def plan(self, query=OFF):
        return (await self.offs(query))[0]

    async def test_run_turns_the_usual_devices_off(self):
        self.history()
        plan = await self.plan()
        response = await self.act(plan, query=OFF)
        self.assertEqual(response.data["status"], "succeeded")
        self.assertEqual(
            self.calls,
            [(e.split(".")[0], "turn_off", {"entity_id": e}) for e in MAJLIS_ON],
        )
        self.assertEqual(self.states.values["climate.majlis_ac"].state, "off")
        # The protected devices are as they were.
        self.assertEqual(self.states.values["switch.majlis_plug"].state, "on")
        self.assertEqual(self.states.values["switch.majlis_heater"].state, "on")
        # The slot is spent.
        self.assertEqual(await self.off_rooms(), [])
        self.assertEqual((await self.act(plan, query=OFF)).status, 409)
        self.assertEqual(len(self.calls), 5)

    async def test_an_action_needs_the_query_that_showed_it(self):
        self.history()
        plan = await self.plan()
        for query in ({}, {"kinds": "room_off,room_eco,usually_on_now"}):
            with self.subTest(query=query):
                self.assertEqual((await self.act(plan, query=query)).status, 409)
        self.assertEqual(self.calls, [])

    async def test_dismiss_and_snooze_are_the_members_own(self):
        self.history()
        plan = await self.plan()
        dismissed = await self.act(plan, "dismiss", query=OFF)
        self.assertEqual(
            dismissed.data,
            {"status": "dismissed", "until": "2026-10-22T18:00:00+00:00"},
        )
        self.assertEqual(await self.off_rooms(), [])
        self.assertEqual(await self.off_rooms(sub="bob:phone"), [("majlis", MAJLIS_ON)])
        [bobs] = await self.offs(sub="bob:x")
        snoozed = await self.act(bobs, "snooze", query=OFF, sub="bob:x")
        self.assertEqual(snoozed.data["status"], "snoozed")
        self.assertEqual(await self.off_rooms(sub="bob:x"), [])
        self.assertEqual(self.calls, [])

    async def test_a_device_switched_off_or_moved_meanwhile_is_left_alone(self):
        self.history()
        plan = await self.plan()
        original = self.hass.services.async_call

        async def call(domain, service, data, blocking=True, context=None):
            await original(domain, service, data, blocking=blocking, context=context)
            if data["entity_id"] == "climate.majlis_ac":
                # Someone switches the lamp off at the wall meanwhile, and
                # moves the relay to the bedroom.
                self.states.values["switch.majlis_lamp"].state = "off"
                self.hass.rooms["switch.majlis_relay"] = "bedroom"

        self.hass.services.async_call = call
        response = await self.act(plan, query=OFF)
        self.assertEqual(response.data["status"], "partial_failure")
        self.assertEqual(
            [d["entity_id"] for _, _, d in self.calls],
            ["climate.majlis_ac", "fan.majlis", "light.majlis"],
        )
        # Their steps were skipped, not sent.
        self.assertEqual(
            (response.data["succeeded_count"], response.data["failed_count"]), (3, 2)
        )


class StepTest(unittest.TestCase):
    """What a usually_off_now step may be: a plain turn_off of a device that
    is on (generated_suggestions.usual_off_step_applies)."""

    def test_usual_off_steps(self):
        def state(entity_id, value, **attributes):
            return NS(entity_id=entity_id, state=value, attributes=attributes)

        def step(entity_id, action="turn_off", **data):
            return {"entity_id": entity_id, "action": action, "data": data}

        ac = {"hvac_modes": ["off", "cool"]}
        applies = generated.usual_off_step_applies
        for entity_id, value, attributes, item, expected in (
            ("light.a", "on", {}, step("light.a"), True),
            ("fan.a", "on", {}, step("fan.a"), True),
            ("switch.a", "on", {}, step("switch.a"), True),
            ("climate.a", "cool", ac, step("climate.a"), True),
            ("climate.a", "dry", ac, step("climate.a"), True),
            # Already off, not a plain off, or left out of room activity.
            ("light.a", "off", {}, step("light.a"), False),
            ("light.a", "unavailable", {}, step("light.a"), False),
            ("climate.a", "off", ac, step("climate.a"), False),
            ("light.a", "on", {}, step("light.a", transition=2), False),
            ("light.a", "on", {}, step("light.a", "turn_on"), False),
            (
                "climate.a",
                "cool",
                ac,
                step("climate.a", "set_hvac_mode", hvac_mode="off"),
                False,
            ),
            (
                "light.a",
                "on",
                {"casasmart_room_activity_exclude": True},
                step("light.a"),
                False,
            ),
            # Not an AC room off may switch off, nor anything else.
            ("climate.a", "cool", {"hvac_modes": ["cool"]}, step("climate.a"), False),
            (
                "climate.a",
                "heat",
                {"hvac_modes": ["off", "heat"]},
                step("climate.a"),
                False,
            ),
            ("cover.a", "open", {}, step("cover.a"), False),
            ("lock.a", "unlocked", {}, step("lock.a"), False),
            ("media_player.a", "on", {}, step("media_player.a"), False),
        ):
            with self.subTest(entity_id=entity_id, state=value, step=item):
                self.assertEqual(
                    applies(state(entity_id, value, **attributes), item), expected
                )


class RefreshTest(OffCase):
    async def test_a_refresh_follows_the_households_turn_offs(self):
        self.history()
        runtime = self.runtime.generated
        self.addCleanup(runtime.stop)
        await runtime.refresh()
        changed = len(self.hass.bus.events)
        await runtime.refresh()
        self.assertEqual(len(self.hass.bus.events), changed)
        # The light goes off at the wall: what the tablet would be offered
        # changes.
        self.states.values["light.majlis"].state = "off"
        await runtime.refresh()
        self.assertEqual(len(self.hass.bus.events), changed + 1)
        # A device held back as just switched on: the refresh looks again
        # when it stops being so (in 5 minutes), sooner than the next hour.
        self.states.values["fan.majlis"].last_changed_timestamp = NOW.timestamp() - 1500
        data, _, _, now, zone = await runtime.refresh_context()
        self.assertEqual(data["_generated_wake_t"], NOW.timestamp() + 300)
        self.assertEqual(
            runtime._refresh_at(data, now, zone), NOW + timedelta(minutes=5)
        )


if __name__ == "__main__":
    unittest.main()
