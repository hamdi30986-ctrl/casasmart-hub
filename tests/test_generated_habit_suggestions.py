"""usually_on_now: generated "turn on" plans from the room habits.

The real suggestion handlers, runtime, scene executor and command whitelist
(test_suggestion_api's fixture), the real ranking and the real HabitLearner
over a real SQLite room activity log. History is written into the log as
the recorder would have logged it; Home Assistant's states are fakes.
"""

from __future__ import annotations

import importlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import test_suggestion_api as api_fixture

API, RUNTIME = api_fixture.API, api_fixture.RUNTIME
learner_module = importlib.import_module("phase4_fixture.room_intel.learner")
now_data_module = importlib.import_module("phase4_fixture.now_data")

RIYADH = 180
# 2026-10-22, a Thursday; day numbers count from 1970-01-01.
TODAY = 20748
H = 19  # 19:00 local
# 19:10 in Riyadh: in the 16:00-18:00 UTC window, an hour before 20:00 local.
NOW = datetime(2026, 10, 22, 16, 10, tzinfo=UTC)
ALL_KINDS = {"kinds": "room_off,room_eco,usually_on_now"}

ROOMS = [
    {"room_id": "living", "name": "Living", "sort_order": 0},
    {"room_id": "majlis", "name": "Majlis", "sort_order": 1},
    {"room_id": "bedroom", "name": "Bedroom", "sort_order": 2},
    {"room_id": "office", "name": "Office", "sort_order": 3},
    {"room_id": "kitchen", "name": "Kitchen", "sort_order": 4},
]
# entity: (room, state, attributes)
DEVICES = {
    "light.living": ("living", "on", {}),
    "light.majlis": ("majlis", "off", {"friendly_name": "Majlis light"}),
    "light.majlis_spot": ("majlis", "off", {}),
    "switch.majlis_relay": ("majlis", "off", {}),
    "switch.majlis_lamp": ("majlis", "off", {}),
    "switch.majlis_plug": ("majlis", "off", {"device_class": "outlet"}),
    "light.bedroom_lamp": ("bedroom", "off", {}),
    "light.bedroom_main": ("bedroom", "off", {}),
    "light.office": ("office", "off", {}),
    "light.kitchen": ("kitchen", "off", {}),
}
# The majlis' untyped relay and its relay typed as a light, by gang type.
GANGS = {"switch.majlis_lamp": {"type": "light"}}


def event(day, hour, entity, source="wall_switch", member=None, action="on"):
    return {
        "t": day * 86400 + hour * 3600 + 300 - RIYADH * 60,
        "utc_offset_min": RIYADH,
        "room_id": DEVICES[entity][0],
        "entity_id": entity,
        "action": action,
        "source": source,
        "member_id": member,
    }


class Activity:
    """The recorder as the ranking reads it: recent events, no loads."""

    started_at = None

    def __init__(self, events=()):
        self.events = list(events)

    async def async_recent_events(self, since_t, room_ids):
        return [e for e in self.events if e["t"] >= since_t]

    def held_meters(self):
        return frozenset()


class HabitSuggestionCase(unittest.IsolatedAsyncioTestCase):
    asyncSetUp_fixture = api_fixture.ApiTest.asyncSetUp
    request_fixture = api_fixture.ApiTest.request

    async def asyncSetUp(self):
        await self.asyncSetUp_fixture()
        self.now = NOW
        self.store.replace_rules(1, [])
        self.hass.config = NS(time_zone="Asia/Riyadh")
        self.states.values = {
            eid: NS(entity_id=eid, state=state, attributes=dict(attributes))
            for eid, (_, state, attributes) in DEVICES.items()
        }
        self.states.async_all = lambda: list(self.states.values.values())
        self.hass.rooms = {eid: room for eid, (room, _, _) in DEVICES.items()}
        self.registry.list_rooms = lambda: [dict(r) for r in ROOMS]
        self.registry.list_user_devices = lambda: [
            {"control_entity_ids": list(DEVICES), "gangs": GANGS}
        ]
        self.policies = {}
        self.runtime.generated = RUNTIME.GeneratedSuggestionRuntime(
            self.hass,
            self.store,
            self.registry,
            NS(
                room_policy=lambda rid: self.policies.get(
                    rid, now_data_module.default_room_policy()
                )
            ),
            clock=lambda: self.now,
        )
        self.learner = learner_module.HabitLearner(
            self.hass,
            self.db.table("now_room_habits"),
            self.db.table("now_habit_config"),
            self.db.room_activity(),
            ramadan_table=self.db.table("now_ramadan_habits"),
            wall_clock=lambda: self.now.timestamp(),
        )
        await self.learner.async_start()
        self.data.room_habits = self.learner
        self.data.room_activity = None
        self.view = API.CasaSmartGeneratedSuggestionsView(self.hass)
        self.actions = API.CasaSmartGeneratedSuggestionActionView(self.hass)

    def history(self, *, days=range(TODAY - 56, TODAY), hour=H):
        """At this hour every day: the majlis light, its untyped relay and its
        light relay at the wall; Alice's bedroom lamp from the app and the
        bedroom's main light at the wall; the office light at the wall on
        three days in four. The kitchen at 07:00 (someone is home)."""
        events = []
        for day in days:
            events += [
                event(day, hour, "light.majlis"),
                event(day, hour, "switch.majlis_relay"),
                event(day, hour, "switch.majlis_lamp"),
                event(day, hour, "light.bedroom_lamp", "app", "alice"),
                event(day, hour, "light.bedroom_main"),
                event(day, 7, "light.kitchen"),
            ]
            if day % 4:
                events.append(event(day, hour, "light.office"))
        self.db.room_activity().append_many(events)

    def request(self, body=None, query=None, **claims):
        request = self.request_fixture(body, **claims)
        request.query = query or {}
        return request

    async def get(self, query=None, **claims):
        response = await self.view.get(self.request(query=query, **claims))
        self.assertEqual(response.status, 200, response.data)
        return response.data

    async def usual(self, query=ALL_KINDS, **claims):
        return [
            (p["room_id"], [a["entity_id"] for a in p["actions"]])
            for p in (await self.get(query, **claims))["suggestions"]
            if p["kind"] == "usually_on_now"
        ]

    async def act(self, plan, action="run", query=ALL_KINDS, **claims):
        return await self.actions.post(
            self.request(
                {"action": action, "occurrence_id": plan["occurrence_id"]},
                query=query,
                **claims,
            )
        )


class CapabilityGateTest(HabitSuggestionCase):
    async def test_without_kinds_the_offer_is_the_one_before_habits(self):
        self.history()
        asked = []
        query = self.learner.query
        self.learner.query = lambda member: asked.append(member) or query(member)
        old_app = await self.get()
        # The same request on a hub without habits.
        self.data.room_habits = None
        no_habits = await self.get()
        self.assertEqual(old_app, no_habits)
        self.assertEqual(old_app, await self.get({"kinds": "room_off,room_eco"}))
        self.assertEqual(asked, [])
        self.assertEqual(
            [(p["kind"], p["room_id"]) for p in old_app["suggestions"]],
            [("room_off", "living")],
        )
        self.assertEqual(set(old_app), {"version", "source", "status", "suggestion",
                                        "suggestions", "refresh_at"})  # fmt: skip
        # The end of the two-hour window, as before.
        self.assertEqual(old_app["refresh_at"], "2026-10-22T18:00:00+00:00")
        self.assertNotIn("usually_on_now", json.dumps(old_app))

    async def test_usually_on_now_only_when_asked_for(self):
        self.history()
        payload = await self.get({**ALL_KINDS, "habits": "household"})
        self.assertEqual(
            [(p["kind"], p["room_id"]) for p in payload["suggestions"]],
            [
                ("room_off", "living"),
                ("usually_on_now", "majlis"),
                ("usually_on_now", "bedroom"),
            ],
        )
        plan = payload["suggestions"][1]
        self.assertEqual(
            {k: plan[k] for k in ("source", "kind", "room_id", "room_name")},
            {
                "source": "generated_room_v1",
                "kind": "usually_on_now",
                "room_id": "majlis",
                "room_name": "Majlis",
            },
        )
        self.assertEqual(
            plan["scene"],
            {"scene_id": plan["occurrence_id"], "name": "Turn on · Majlis",
             "icon": "lightbulb"},
        )  # fmt: skip
        self.assertEqual(
            plan["actions"],
            [
                {"entity_id": "light.majlis", "action": "turn_on", "data": {},
                 "name": "Majlis light"},
                {"entity_id": "switch.majlis_lamp", "action": "turn_on", "data": {},
                 "name": "switch.majlis_lamp"},
            ],
        )  # fmt: skip
        self.assertEqual(
            plan["reason"], {"code": "usually_on_now", "layer": "household", "hour": H}
        )
        self.assertEqual(plan["expires_at"], "2026-10-22T18:00:00+00:00")
        self.assertEqual(len(plan["occurrence_id"]), 64)
        self.assertEqual(plan["suppression_id"], plan["rule_id"])
        # A habit is an hour's: look again at 20:00 local.
        self.assertEqual(payload["refresh_at"], "2026-10-22T17:00:00+00:00")
        self.assertEqual(self.calls, [])

    async def test_the_kinds_pick_what_is_offered(self):
        self.history()
        only = await self.get({"kinds": "usually_on_now", "habits": "household"})
        self.assertEqual(
            [(p["kind"], p["room_id"]) for p in only["suggestions"]],
            [("usually_on_now", "majlis"), ("usually_on_now", "bedroom")],
        )
        self.assertEqual(only["suggestion"], only["suggestions"][0])
        off = await self.get({"kinds": "room_off"})
        self.assertEqual([p["kind"] for p in off["suggestions"]], ["room_off"])
        # A kind this hub doesn't know (a newer app's) is skipped.
        newer = await self.get(
            {"kinds": " usually_on_now , usually_off_by_now", "habits": "household"}
        )
        self.assertEqual(newer["suggestions"], only["suggestions"])
        self.assertEqual(
            (await self.get({"kinds": "usually_off_by_now"}))["status"], "no_match"
        )

    async def test_bad_kinds_or_habits_are_a_400(self):
        for query, code in (
            ({"kinds": ""}, "invalid_kinds"),
            ({"kinds": " , "}, "invalid_kinds"),
            ({"kinds": "room_off," * 40}, "invalid_kinds"),
            ({"habits": "everyone"}, "invalid_habits"),
            ({**ALL_KINDS, "habits": "Household"}, "invalid_habits"),
        ):
            with self.subTest(query=query):
                response = await self.view.get(self.request(query=query))
                self.assertEqual(response.status, 400)
                self.assertEqual(response.data["error"], code)
                self.assertTrue(response.data["message"])

    async def test_the_handshake_advertises_it(self):
        capabilities = importlib.import_module("phase4_fixture.capabilities")
        feature = capabilities.handshake_capabilities()["features"][
            "generated_room_habit_suggestions_v1"
        ]
        self.assertEqual(
            feature, {"version": 1, "minimum_api_version": 1, "available": True}
        )


class HabitLayerTest(HabitSuggestionCase):
    async def test_the_tablet_gets_the_households_and_a_phone_its_members(self):
        self.history()
        household = await self.get({**ALL_KINDS, "habits": "household"})
        self.assertEqual(
            [(p["room_id"], [a["entity_id"] for a in p["actions"]])
             for p in household["suggestions"] if p["kind"] == "usually_on_now"],
            [("majlis", ["light.majlis", "switch.majlis_lamp"]),
             ("bedroom", ["light.bedroom_lamp", "light.bedroom_main"])],
        )  # fmt: skip
        # Alice's own taps are the bedroom lamp: the majlis and the bedroom's
        # wall-pressed main light aren't hers.
        mine = await self.get(ALL_KINDS)
        usual = [p for p in mine["suggestions"] if p["kind"] == "usually_on_now"]
        self.assertEqual(
            [(p["room_id"], [a["entity_id"] for a in p["actions"]]) for p in usual],
            [("bedroom", ["light.bedroom_lamp"])],
        )
        self.assertEqual(usual[0]["reason"]["layer"], "personal")
        self.assertEqual(
            await self.usual({**ALL_KINDS, "habits": "personal"}),
            [("bedroom", ["light.bedroom_lamp"])],
        )
        # Bob never used the app: his phone gets the household's.
        self.assertEqual(
            await self.usual(sub="bob:phone"),
            [("majlis", ["light.majlis", "switch.majlis_lamp"]),
             ("bedroom", ["light.bedroom_lamp", "light.bedroom_main"])],
        )  # fmt: skip

    async def test_a_scoped_member_gets_only_their_rooms(self):
        self.history()
        self.assertEqual(
            await self.usual(rooms=["majlis", "kitchen"], sub="bob:phone"),
            [("majlis", ["light.majlis", "switch.majlis_lamp"])],
        )
        self.assertEqual(await self.usual(rooms=[]), [])

    async def test_no_habits_no_turn_on(self):
        # Under two weeks of history: warming up.
        self.history(days=range(TODAY - 10, TODAY))
        self.assertEqual(await self.usual({**ALL_KINDS, "habits": "household"}), [])
        self.history()
        self.learner._drop_model()
        await self.learner.async_update({"learning": False}, managed_ids=())
        self.assertEqual(await self.usual({**ALL_KINDS, "habits": "household"}), [])

    async def test_a_wall_only_room_gets_a_turn_on_at_the_weekend(self):
        # Friday 19:10. The majlis is only ever switched on at the wall, on
        # weekend evenings; someone is home every day.
        self.now = NOW + timedelta(days=1)
        friday = TODAY + 1
        days = range(friday - 56, friday)
        events = [event(day, 7, "light.kitchen") for day in days]
        events += [
            event(day, H, "light.majlis") for day in days if (day + 3) % 7 in (4, 5)
        ]
        self.db.room_activity().append_many(events)
        self.assertEqual(
            await self.usual({**ALL_KINDS, "habits": "household"}),
            [("majlis", ["light.majlis"])],
        )


class PlanTest(HabitSuggestionCase):
    async def test_only_the_usual_lights_and_fans_that_are_off(self):
        self.history()
        # The spot never comes on at this hour; the untyped relay does but one
        # tap never turns an untyped relay on; the plug never counts.
        self.states.values["light.majlis"].state = "unavailable"
        self.assertEqual(
            await self.usual({**ALL_KINDS, "habits": "household"}),
            [("majlis", ["switch.majlis_lamp"]),
             ("bedroom", ["light.bedroom_lamp", "light.bedroom_main"])],
        )  # fmt: skip
        # A device left out of room activity is left alone.
        self.states.values["switch.majlis_lamp"].attributes[
            "casasmart_room_activity_exclude"
        ] = True
        self.assertEqual(
            await self.usual({**ALL_KINDS, "habits": "household"}),
            [("bedroom", ["light.bedroom_lamp", "light.bedroom_main"]),
             ("office", ["light.office"])],
        )  # fmt: skip

    async def test_a_room_that_is_on_or_was_just_switched_off_gets_none(self):
        self.history()
        self.states.values["light.majlis_spot"].state = "on"
        self.data.room_activity = Activity(
            [
                {**event(TODAY, H, "light.bedroom_main", action="off"),
                 "t": int(self.now.timestamp()) - 300}
            ]
        )  # fmt: skip
        self.assertEqual(
            await self.usual({**ALL_KINDS, "habits": "household"}),
            [("office", ["light.office"])],
        )

    async def test_a_room_out_of_room_commands_gets_none(self):
        self.history()
        self.policies["majlis"] = {
            "participates": False,
            "eligible_entity_ids": [],
            "default": False,
        }
        self.assertEqual(
            await self.usual({**ALL_KINDS, "habits": "household"}),
            [("bedroom", ["light.bedroom_lamp", "light.bedroom_main"]),
             ("office", ["light.office"])],
        )  # fmt: skip
        # A stored policy that takes part turns on only what it approves.
        self.policies["majlis"] = {
            "participates": True,
            "eligible_entity_ids": ["switch.majlis_lamp"],
            "default": False,
        }
        self.assertEqual(
            (await self.usual({**ALL_KINDS, "habits": "household"}))[0],
            ("majlis", ["switch.majlis_lamp"]),
        )

    async def test_at_most_two_rooms_strongest_first(self):
        self.history()
        self.assertEqual(
            [r for r, _ in await self.usual({**ALL_KINDS, "habits": "household"})],
            ["majlis", "bedroom"],
        )
        # With the majlis on, the office (the next strongest) comes in.
        self.states.values["light.majlis_spot"].state = "on"
        self.assertEqual(
            [r for r, _ in await self.usual({**ALL_KINDS, "habits": "household"})],
            ["bedroom", "office"],
        )

    async def test_energy_saving_hides_every_turn_on(self):
        self.history()
        self.data.energy = NS(locked=False, active_level="medium")
        payload = await self.get({**ALL_KINDS, "habits": "household"})
        self.assertEqual([p["kind"] for p in payload["suggestions"]], ["room_off"])
        # Without habits the next refresh is the window's end again.
        self.assertEqual(payload["refresh_at"], "2026-10-22T18:00:00+00:00")


class RunTest(HabitSuggestionCase):
    async def plan(self, room="majlis", query=None):
        query = query or {**ALL_KINDS, "habits": "household"}
        return next(
            p
            for p in (await self.get(query))["suggestions"]
            if p["kind"] == "usually_on_now" and p["room_id"] == room
        )

    async def test_run_turns_the_usual_devices_on(self):
        self.history()
        plan = await self.plan()
        response = await self.act(plan, query={**ALL_KINDS, "habits": "household"})
        self.assertEqual(response.data["status"], "succeeded")
        self.assertEqual(
            self.calls,
            [
                ("light", "turn_on", {"entity_id": "light.majlis"}),
                ("switch", "turn_on", {"entity_id": "switch.majlis_lamp"}),
            ],
        )
        # The majlis is on now: no turn on for it, and the slot is spent.
        self.assertNotIn(
            "majlis", [r for r, _ in await self.usual({**ALL_KINDS, "habits": "household"})]
        )  # fmt: skip
        again = await self.act(plan, query={**ALL_KINDS, "habits": "household"})
        self.assertEqual(again.status, 409)
        self.assertEqual(len(self.calls), 2)

    async def test_an_action_needs_the_query_that_showed_it(self):
        self.history()
        plan = await self.plan()
        # Without ?kinds= there is no such plan; with Alice's own habits the
        # majlis isn't usual.
        for query in ({}, ALL_KINDS):
            with self.subTest(query=query):
                self.assertEqual((await self.act(plan, query=query)).status, 409)
        self.assertEqual(
            (await self.act(plan, query={"kinds": "x", "habits": "bad"})).status, 400
        )
        self.assertEqual(self.calls, [])
        mine = await self.plan("bedroom", ALL_KINDS)
        self.assertEqual((await self.act(mine, query=ALL_KINDS)).status, 200)
        self.assertEqual(
            self.calls, [("light", "turn_on", {"entity_id": "light.bedroom_lamp"})]
        )

    async def test_dismiss_is_the_members_own(self):
        self.history()
        plan = await self.plan()
        query = {**ALL_KINDS, "habits": "household"}
        dismissed = await self.act(plan, "dismiss", query=query)
        self.assertEqual(dismissed.data["status"], "dismissed")
        self.assertEqual(dismissed.data["until"], "2026-10-22T18:00:00+00:00")
        self.assertNotIn("majlis", [r for r, _ in await self.usual(query)])
        self.assertIn("majlis", [r for r, _ in await self.usual(query, sub="bob:x")])

    async def test_a_device_switched_on_meanwhile_is_left_alone(self):
        self.history()
        plan = await self.plan()
        original = self.hass.services.async_call

        async def call(domain, service, data, blocking=True, context=None):
            await original(domain, service, data, blocking=blocking, context=context)
            # Someone switches the lamp relay on at the wall meanwhile.
            self.states.values["switch.majlis_lamp"].state = "on"

        self.hass.services.async_call = call
        response = await self.act(plan, query={**ALL_KINDS, "habits": "household"})
        self.assertEqual(response.data["status"], "partial_failure")
        self.assertEqual(
            self.calls, [("light", "turn_on", {"entity_id": "light.majlis"})]
        )

    async def test_energy_saving_refuses_a_shown_turn_on(self):
        self.history()
        plan = await self.plan()
        self.data.energy = NS(locked=False, active_level="medium")
        response = await self.act(plan, query={**ALL_KINDS, "habits": "household"})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.calls, [])
        # Were it still offered, its scene doesn't run during Energy Saving.
        self.data.energy = None
        context = await self.runtime.generated.context(
            None, RUNTIME.Ask(frozenset({"usually_on_now"}), household=True)
        )
        self.assertEqual(
            {s["kind"]: s["works_during_energy_saving"] for s in context[1].values()},
            {"usually_on_now": False},
        )


class RefreshTest(HabitSuggestionCase):
    async def test_a_refresh_follows_the_households_turn_ons(self):
        self.history()
        generated = self.runtime.generated
        self.addCleanup(generated.stop)
        await generated.refresh()
        changed = len(self.hass.bus.events)
        self.assertGreater(changed, 0)
        # Looked at again within the minute, as every suggestion is.
        self.assertLessEqual(generated._timer.when() - generated.hass.loop.time(), 60)
        await generated.refresh()
        self.assertEqual(len(self.hass.bus.events), changed)
        # The majlis comes on: what the tablet would be offered changes.
        self.states.values["light.majlis"].state = "on"
        await generated.refresh()
        self.assertEqual(len(self.hass.bus.events), changed + 1)
        # 21:10: nothing is usual at this hour any more.
        self.now = NOW + timedelta(hours=2)
        await generated.refresh()
        self.assertEqual(len(self.hass.bus.events), changed + 2)
        self.assertEqual(await self.usual({**ALL_KINDS, "habits": "household"}), [])


if __name__ == "__main__":
    unittest.main()
