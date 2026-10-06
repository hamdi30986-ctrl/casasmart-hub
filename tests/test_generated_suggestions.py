"""Generated scenes: pure policy plus real handlers, SQLite and executor."""

import unittest
from datetime import timedelta
from types import SimpleNamespace as NS

import test_suggestion_api as api_fixture
from phase4_fixture.generated_suggestions import room_actions

API, RUNTIME = api_fixture.API, api_fixture.RUNTIME


def light(eid, **attrs):
    return NS(entity_id=eid, state="on", attributes=attrs)


def ac(**attrs):
    return NS(
        entity_id="climate.ac",
        state="cool",
        attributes={
            "hvac_modes": ["off", "cool", "heat"],
            "temperature": 21,
            "supported_features": 9,
            "fan_modes": ["low", "high"],
            "fan_mode": "high",
            **attrs,
        },
    )


class PolicyTest(unittest.TestCase):
    def test_strict_domains_and_cooling_capability(self):
        states = [
            light("light.one"),
            ac(),
            light("switch.plug"),
            light("fan.one"),
            light("cover.one"),
            NS(
                entity_id="climate.heater",
                state="heat",
                attributes={"hvac_modes": ["heat", "off"]},
            ),
        ]
        self.assertEqual(
            [a["entity_id"] for a in room_actions(states, "room_off")],
            ["light.one", "climate.ac"],
        )

    def test_eco_never_brightens_or_turns_every_light_off(self):
        states = [
            light("light.dim", brightness=200, supported_color_modes=["brightness"]),
            light("light.low", brightness=40, supported_color_modes=["brightness"]),
            light("light.a"),
            light("light.b"),
            ac(),
        ]
        actions = room_actions(states, "room_eco")
        self.assertNotIn("light.low", [a["entity_id"] for a in actions])
        self.assertEqual(
            [a["action"] for a in actions if a["entity_id"] == "climate.ac"],
            ["set_temperature", "set_fan_mode"],
        )
        self.assertEqual(room_actions([light("light.single")], "room_eco"), [])

    def test_temperature_units_modes_ranges_and_capabilities(self):
        for state in [
            ac(temperature=26),
            ac(supported_features=0),
            ac(min_temp=25),
            ac(target_temp_step=5),
        ]:
            self.assertNotIn(
                "set_temperature",
                [a["action"] for a in room_actions([state], "room_eco")],
            )
        state = ac()
        state.state = "heat"
        self.assertEqual(room_actions([state], "room_eco"), [])
        actions = room_actions(
            [ac(temperature=70, temperature_unit="°F", target_temp_step=0.1)],
            "room_eco",
        )
        self.assertEqual(actions[0]["data"], {"temperature": 75.2})


class GeneratedApiTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_fixture.ApiTest.asyncSetUp
    request = api_fixture.ApiTest.request

    async def setup_generated(self):
        self.states.values = {
            "light.one": light(
                "light.one", brightness=200, supported_color_modes=["brightness"]
            ),
            "light.two": light("light.two"),
            "light.other": light("light.other"),
            "climate.ac": ac(),
            "switch.plug": light("switch.plug"),
        }
        self.states.async_all = lambda: list(self.states.values.values())
        self.hass.rooms = {
            eid: "b" if eid == "light.other" else "a" for eid in self.states.values
        }
        self.registry.list_rooms = lambda: [
            {"room_id": "a", "name": "Living"},
            {"room_id": "b", "name": "Kitchen"},
        ]
        self.registry.list_user_devices = lambda: [
            {"control_entity_ids": list(self.states.values)}
        ]
        self.runtime.generated = RUNTIME.GeneratedSuggestionRuntime(
            self.hass,
            self.store,
            self.registry,
            NS(room_policy=lambda rid: {}),
            clock=lambda: self.now,
        )
        self.generated_view = API.CasaSmartGeneratedSuggestionsView(self.hass)
        self.generated_actions = API.CasaSmartGeneratedSuggestionActionView(self.hass)

    async def plans(self, **claims):
        return (await self.generated_view.get(self.request(**claims))).data[
            "suggestions"
        ]

    async def run_plan(self, plan, action="run", **claims):
        return await self.generated_actions.post(
            self.request(
                {"action": action, "occurrence_id": plan["occurrence_id"]}, **claims
            )
        )

    async def test_three_without_saved_scenes_and_explicit_run_both_ac_commands(self):
        await self.setup_generated()
        self.scenes = []
        self.store.replace_rules(1, [])
        plans = await self.plans()
        self.assertEqual(
            [p["kind"] for p in plans], ["room_off", "room_eco", "room_off"]
        )
        self.assertEqual([p["room_id"] for p in plans], ["a", "a", "b"])
        self.assertEqual(self.calls, [])
        result = await self.run_plan(plans[1])
        self.assertEqual(result.data["status"], "succeeded")
        self.assertEqual(
            [c[1] for c in self.calls if c[0] == "climate"],
            ["set_temperature", "set_fan_mode"],
        )
        self.assertFalse(any(c[0] == "switch" for c in self.calls))

    async def test_two_hours_freezes_rooms_not_device_state(self):
        await self.setup_generated()
        original = await self.plans()
        self.states.values["light.one"].state = "off"
        new = await self.plans()
        self.assertEqual(new[0]["room_id"], "a")
        self.assertNotEqual(new[0]["occurrence_id"], original[0]["occurrence_id"])
        self.assertEqual((await self.run_plan(original[0])).status, 409)
        self.now += timedelta(hours=2)
        self.assertNotEqual(
            (await self.plans())[0]["suppression_id"], original[0]["suppression_id"]
        )

    async def test_scope_dismissal_changes_and_read_only(self):
        await self.setup_generated()
        self.assertEqual({p["room_id"] for p in await self.plans(rooms=["b"])}, {"b"})
        self.assertEqual(await self.plans(rooms=[]), [])
        plan = (await self.plans())[0]
        self.assertEqual((await self.run_plan(plan, control=False)).status, 403)
        await self.run_plan(plan, "dismiss")
        self.states.values["light.one"].state = "off"
        self.assertNotIn(
            plan["suppression_id"], [p["suppression_id"] for p in await self.plans()]
        )
        self.assertEqual(self.calls, [])

    async def test_move_during_claim_aborts_without_commands(self):
        await self.setup_generated()
        plan = (await self.plans())[0]
        original = self.hass.async_add_executor_job

        async def executor(fn, *args):
            result = await original(fn, *args)
            if fn.__name__ == "claim":
                self.hass.rooms["light.one"] = "b"
            return result

        self.hass.async_add_executor_job = executor
        self.assertEqual((await self.run_plan(plan)).status, 409)
        self.assertEqual(self.calls, [])

    async def test_slot_claim_blocks_changed_payload_and_restart(self):
        await self.setup_generated()
        plan = (await self.plans())[1]
        self.store.claim(plan, self.now)
        self.store.recover()
        self.states.values["climate.ac"].attributes["temperature"] = 24
        self.assertNotIn(
            plan["suppression_id"], [p["suppression_id"] for p in await self.plans()]
        )
        claimed, receipt = self.store.claim(
            {**plan, "occurrence_id": "x" * 64}, self.now
        )
        self.assertFalse(claimed)
        self.assertEqual(receipt["status"], "unknown")

    async def test_empty_startup_fills_slots_when_activity_arrives(self):
        await self.setup_generated()
        for state in self.states.values.values():
            state.state = "off"
        self.assertEqual(await self.plans(), [])
        self.states.values["light.one"].state = "on"
        self.assertEqual((await self.plans())[0]["room_id"], "a")
        self.states.values["light.other"].state = "on"
        self.assertEqual((await self.plans())[-1]["room_id"], "b")

    async def test_rank_ties_and_configured_policy(self):
        await self.setup_generated()
        self.states.values["light.two"].state = "off"
        ranked, _, _ = await self.runtime.generated.room_context(None)
        self.assertEqual([r["room_id"] for r in ranked], ["a", "b"])
        self.runtime.generated.now_data.room_policy = lambda rid: {
            "participates": rid == "b",
            "eligible_entity_ids": ["light.other"],
        }
        self.assertEqual({p["room_id"] for p in await self.plans()}, {"b"})

    async def test_unimported_and_hidden_controls_never_rank_or_execute(self):
        await self.setup_generated()
        ids = list(self.states.values)
        self.registry.list_user_devices = lambda: [
            {
                "control_entity_ids": ids,
                "gangs": {"light.two": {"presentation": "hidden"}},
                "config_entity_ids": ["climate.ac"],
            }
        ]
        self.states.values["light.unimported"] = light("light.unimported")
        self.hass.rooms["light.unimported"] = "b"
        ranked, _, _ = await self.runtime.generated.room_context(None)
        self.assertEqual(
            [(r["room_id"], r["active_count"]) for r in ranked], [("a", 1), ("b", 1)]
        )
        actions = [a for p in await self.plans() for a in p["actions"]]
        self.assertFalse(
            {"light.unimported", "light.two", "climate.ac"}
            & {a["entity_id"] for a in actions}
        )

    async def test_legacy_gang_suffixes_rank_but_never_enter_actions(self):
        await self.setup_generated()
        self.states.values["switch.relay_left"] = light("switch.relay_left")
        self.hass.rooms["switch.relay_left"] = "b"
        self.registry.list_user_devices = lambda: [
            {
                "control_entity_ids": list(self.states.values),
                "gang_types": {"left": "switch"},
            },
        ]
        ranked, _, _ = await self.runtime.generated.room_context(None)
        self.assertEqual(
            [(r["room_id"], r["active_count"]) for r in ranked], [("a", 2), ("b", 2)]
        )
        self.assertFalse(
            any(
                a["entity_id"].startswith("switch.")
                for p in await self.plans()
                for a in p["actions"]
            )
        )

    async def test_energy_lockout_and_expiry_never_dispatch(self):
        await self.setup_generated()
        plan = (await self.plans())[0]
        self.data.energy = NS(locked=True, active_level=None)
        self.assertEqual((await self.run_plan(plan)).status, 403)
        self.data.energy = None
        self.now += timedelta(hours=2)
        self.assertEqual((await self.run_plan(plan)).status, 409)
        self.assertEqual(self.calls, [])

    async def test_state_change_between_commands_cannot_reenable_light(self):
        await self.setup_generated()
        plan = (await self.plans())[0]
        original = self.hass.services.async_call

        async def call(domain, service, data, blocking=True):
            await original(domain, service, data, blocking=blocking)
            self.hass.rooms["climate.ac"] = "b"

        self.hass.services.async_call = call
        response = await self.run_plan(plan)
        self.assertEqual(response.data["status"], "partial_failure")
        self.assertFalse(any(c[0] == "climate" for c in self.calls))
