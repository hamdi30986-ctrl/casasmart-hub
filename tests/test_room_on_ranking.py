"""The "something is on" room ranking: watts or count, ACs, fillers, ties.

Runs without Home Assistant, on the suggestion-runtime fixture the golden
ranking pins use, so the real room_intel.ranking and room_intel.meters run
over fake states, a fake registry and the fixture's filtering (served = has a
state, room = the fixture's room map).
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import test_suggestion_api as api_fixture
import test_suggestions as fixtures

RUNTIME = api_fixture.RUNTIME
# The ranking module the fixture's suggestion runtime was loaded with (the
# fixture's stub filtering only exists while it loads).
_RANKING = RUNTIME.async_rank_rooms.__globals__
ranking_summary = _RANKING["ranking_summary"]
NOW_ROOM_SLOTS = _RANKING["NOW_ROOM_SLOTS"]

AC_MODES = ["off", "cool", "heat", "dry", "fan_only"]


def light(eid, state="on"):
    return eid, state, {}


def relay(eid, state="on", gang="light"):
    """A relay channel (switch) with its gang type."""
    return eid, state, {"device_class": "switch"}, gang


def ac(eid, state="cool", **attributes):
    return eid, state, {"hvac_modes": AC_MODES, **attributes}


def power(eid, value, unit="W"):
    return eid, value, {"device_class": "power", "unit_of_measurement": unit}


class OnRankingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rooms = []
        self.values = {}
        self.areas = {}
        self.devices = []
        self.policies = {}

    def room(self, room_id, name, sort_order=0):
        self.rooms.append({"room_id": room_id, "name": name, "sort_order": sort_order})

    def device(self, room_id, *entities, config=()):
        """One registry device in a room: (entity_id, state, attributes[, gang])."""
        record = {"control_entity_ids": [], "config_entity_ids": [], "gangs": {}}
        for entity in entities:
            self.add_state(room_id, entity)
            record["control_entity_ids"].append(entity[0])
            if len(entity) == 4:
                record["gangs"][entity[0]] = {"type": entity[3]}
        for entity in config:
            self.add_state(room_id, entity)
            record["control_entity_ids"].append(entity[0])
            record["config_entity_ids"].append(entity[0])
        self.devices.append(record)
        return record

    def add_state(self, room_id, entity):
        eid, state, attributes = entity[:3]
        self.values[eid] = NS(entity_id=eid, state=state, attributes=dict(attributes))
        self.areas[eid] = room_id

    def set(self, eid, state):
        self.values[eid].state = state

    async def ranked(self, scope=None):
        async def executor(fn, *args):
            return fn(*args)

        values = self.values
        hass = NS(
            states=NS(values=values, async_all=lambda: list(values.values())),
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
        runtime = RUNTIME.GeneratedSuggestionRuntime(hass, None, registry, now_data)
        ranked, _, _ = await runtime.room_context(scope)
        return ranked

    async def order(self, scope=None):
        return [r["room_id"] for r in await self.ranked(scope)]

    async def ranks(self, scope=None):
        return {r["room_id"]: r["rank"] for r in await self.ranked(scope)}

    async def basis(self, scope=None):
        return ranking_summary(await self.ranked(scope))["basis"]

    # -- a metered home -------------------------------------------------------

    def metered_home(self):
        """Majlis: an AC drawing 1.84 kW; Bedroom: a 120 W plug; Kitchen:
        three 10 W relay lights. Every device has its own meter."""
        self.room("majlis", "Majlis")
        self.room("kitchen", "Kitchen")
        self.room("bedroom", "Bedroom")
        self.device(
            "majlis", ac("climate.majlis"), power("sensor.majlis_ac", "1.84", "kW")
        )
        self.device(
            "bedroom",
            relay("switch.bed_plug", gang="outlet"),
            power("sensor.bed_plug", "120"),
        )
        for index in range(3):
            self.device(
                "kitchen",
                relay(f"switch.kitchen_{index}"),
                power(f"sensor.kitchen_{index}", "10"),
            )

    async def test_a_fully_metered_home_ranks_by_watts(self):
        self.metered_home()
        # By count, Kitchen (3 on) would lead.
        self.assertEqual(await self.order(), ["majlis", "bedroom", "kitchen"])
        self.assertEqual(await self.basis(), "watts")
        ranks = await self.ranks()
        self.assertEqual(
            ranks["majlis"],
            {
                "active": True,
                "filler": False,
                "on_count": 1,
                "ac_on_count": 1,
                "watts": 1840.0,
                "watts_quality": "complete",
                # Why it's where it is.
                "reason": "active",
                "params": {},
            },
        )
        self.assertEqual(
            (ranks["kitchen"]["on_count"], ranks["kitchen"]["watts"]), (3, 30.0)
        )
        self.assertEqual(ranks["bedroom"]["watts"], 120.0)

    async def test_a_partly_metered_home_ranks_the_whole_list_by_count(self):
        self.metered_home()
        # One more Kitchen light, a bulb with no meter.
        self.device("kitchen", light("light.kitchen_bulb"))
        self.assertEqual(await self.basis(), "count")
        # Kitchen 4 on; Majlis and Bedroom tie on 1 and go by name.
        self.assertEqual(await self.order(), ["kitchen", "bedroom", "majlis"])
        ranks = await self.ranks()
        self.assertEqual(
            (ranks["kitchen"]["watts"], ranks["kitchen"]["watts_quality"]),
            (30.0, "partial"),
        )
        # The metered rooms still report their watts.
        self.assertEqual(
            (ranks["majlis"]["watts"], ranks["majlis"]["watts_quality"]),
            (1840.0, "complete"),
        )

    async def test_an_off_device_without_a_meter_does_not_spoil_coverage(self):
        self.metered_home()
        self.device("kitchen", light("light.kitchen_bulb", "off"))
        self.assertEqual(await self.basis(), "watts")
        self.assertEqual(await self.order(), ["majlis", "bedroom", "kitchen"])

    async def test_a_home_without_meters_ranks_by_count(self):
        self.room("a", "Majlis")
        self.room("b", "Kitchen")
        self.device("a", light("light.a1"))
        self.device("b", light("light.b1"), light("light.b2"))
        self.assertEqual(await self.order(), ["b", "a"])
        self.assertEqual(await self.basis(), "count")
        for rank in (await self.ranks()).values():
            self.assertEqual((rank["watts"], rank["watts_quality"]), (None, "absent"))

    async def test_an_unreadable_meter_falls_back_to_count(self):
        for state in ["unavailable", "unknown", "abc", "-50"]:
            with self.subTest(state=state):
                self.setUp()
                self.metered_home()
                self.set("sensor.kitchen_1", state)
                self.assertEqual(await self.basis(), "count")
                kitchen = (await self.ranks())["kitchen"]
                # The two good meters still sum, but they don't cover the room.
                self.assertEqual(
                    (kitchen["watts"], kitchen["watts_quality"]), (20.0, "partial")
                )

    async def test_a_room_whose_only_meter_is_unavailable_has_no_watts(self):
        self.metered_home()
        self.set("sensor.bed_plug", "unavailable")
        bedroom = (await self.ranks())["bedroom"]
        self.assertEqual((bedroom["watts"], bedroom["watts_quality"]), (None, "absent"))
        self.assertEqual(await self.basis(), "count")

    async def test_one_bad_meter_on_a_two_channel_device_uncovers_it(self):
        self.room("a", "Majlis")
        self.device(
            "a",
            relay("switch.a_ch1"),
            relay("switch.a_ch2", "off"),
            power("sensor.a_ch1", "40"),
            power("sensor.a_ch2", "unavailable"),
        )
        rank = (await self.ranks())["a"]
        self.assertEqual((rank["watts"], rank["watts_quality"]), (40.0, "partial"))

    async def test_standalone_and_whole_home_meters_never_join_a_room(self):
        self.metered_home()
        # A whole-home clamp whose HA area is the Majlis, and a room CT: no
        # device of theirs is a light, fan, switch or AC.
        self.device("majlis", power("sensor.house_total", "8200"))
        self.device("kitchen", power("sensor.kitchen_ct", "500"))
        ranks = await self.ranks()
        self.assertEqual(ranks["majlis"]["watts"], 1840.0)
        self.assertEqual(ranks["kitchen"]["watts"], 30.0)
        self.assertEqual(await self.basis(), "watts")

    async def test_a_meter_in_another_room_than_its_control_is_not_used(self):
        self.room("a", "Majlis")
        self.room("b", "Kitchen")
        self.device("a", relay("switch.a_light"), power("sensor.a_light", "60"))
        self.areas["sensor.a_light"] = "b"
        ranks = await self.ranks()
        self.assertEqual(
            (ranks["a"]["watts"], ranks["a"]["watts_quality"]), (None, "absent")
        )
        self.assertNotIn("b", ranks)

    async def test_a_diagnostic_meter_counts_and_power_lookalikes_do_not(self):
        self.room("a", "Majlis")
        self.device(
            "a",
            relay("switch.a_plug", gang="outlet"),
            ("sensor.a_plug_power_factor", "0.9", {"device_class": "power_factor"}),
            config=[power("sensor.a_plug_power", "75")],
        )
        rank = (await self.ranks())["a"]
        self.assertEqual((rank["watts"], rank["watts_quality"]), (75.0, "complete"))

    async def test_negative_noise_reads_zero(self):
        self.room("a", "Majlis")
        self.device("a", relay("switch.a"), power("sensor.a", "-1.5"))
        rank = (await self.ranks())["a"]
        self.assertEqual((rank["watts"], rank["watts_quality"]), (0.0, "complete"))

    # -- ACs --------------------------------------------------------------------

    async def test_a_room_with_only_its_ac_on_is_on(self):
        self.room("a", "Majlis")
        self.room("b", "Bedroom")
        self.device("a", light("light.a"))
        self.device("b", ac("climate.b"), light("light.b", "off"))
        ranked = await self.ranked()
        self.assertEqual([r["room_id"] for r in ranked], ["b", "a"])
        bedroom = ranked[0]
        # active_count keeps its meaning: the safe devices room commands switch.
        self.assertEqual(bedroom["active_count"], 0)
        self.assertEqual(bedroom["eligible_entity_ids"], ["light.b"])
        self.assertEqual(
            (
                bedroom["rank"]["active"],
                bedroom["rank"]["on_count"],
                bedroom["rank"]["ac_on_count"],
            ),
            (True, 1, 1),
        )

    async def test_which_acs_count_as_on(self):
        self.room("a", "Room")
        cases = [
            ("cool", {}, True),
            ("heat", {}, True),
            ("dry", {}, True),
            ("fan_only", {}, True),
            ("auto", {}, True),
            ("heat_cool", {}, True),
            ("off", {}, False),
            ("unavailable", {}, False),
            ("unknown", {}, False),
            # A heating-only thermostat is not an AC.
            ("heat", {"hvac_modes": ["off", "heat"]}, False),
            ("cool", {"hvac_modes": None}, False),
            ("cool", {"casasmart_room_activity_exclude": True}, False),
        ]
        for state, attributes, on in cases:
            with self.subTest(state=state, attributes=attributes):
                self.devices = []
                self.values = {}
                self.device("a", ac("climate.a", state, **attributes))
                ranks = await self.ranks()
                self.assertEqual("a" in ranks and ranks["a"]["active"], on)

    async def test_every_running_ac_is_one_device(self):
        self.room("a", "Majlis")
        self.room("b", "Kitchen")
        self.device("a", ac("climate.a1"), ac("climate.a2", "dry"))
        self.device("b", light("light.b1"))
        ranked = await self.ranked()
        self.assertEqual([r["room_id"] for r in ranked], ["a", "b"])
        self.assertEqual(
            (ranked[0]["rank"]["on_count"], ranked[0]["rank"]["ac_on_count"]), (2, 2)
        )

    async def test_an_ac_counts_in_a_room_with_a_policy(self):
        self.room("a", "Majlis")
        self.room("b", "Bedroom")
        self.device("a", light("light.a"))
        self.device("b", ac("climate.b"))
        self.policies = {
            "a": {"participates": True, "eligible_entity_ids": ["light.a"]},
            "b": {"participates": True, "eligible_entity_ids": []},
        }
        ranked = await self.ranked()
        self.assertEqual([r["room_id"] for r in ranked], ["b", "a"])
        self.assertEqual(ranked[0]["rank"]["ac_on_count"], 1)

    # -- fillers ----------------------------------------------------------------

    async def test_one_room_on_and_four_off_rooms_fill_the_slots(self):
        self.room("on", "Zeta", 9)
        for room_id, name, sort_order in [
            ("r1", "Office", 1),
            ("r2", "Guest", 0),
            ("r3", "Attic", 0),
            ("r4", "Hall", 2),
            ("r5", "Yard", 3),
        ]:
            self.room(room_id, name, sort_order)
            self.device(room_id, light(f"light.{room_id}", "off"))
        self.room("dead", "Annex")
        self.device("dead", light("light.dead", "unavailable"))
        self.room("empty", "Atrium")
        self.device("on", light("light.on"))
        ranked = await self.ranked()
        # On rooms first, then the room order: sort_order, then name. Rooms
        # with nothing to switch (unavailable only, no device) never fill.
        self.assertEqual([r["room_id"] for r in ranked], ["on", "r3", "r2", "r1", "r4"])
        self.assertEqual(
            [(r["rank"]["active"], r["rank"]["filler"]) for r in ranked],
            [(True, False)] + [(False, True)] * 4,
        )
        self.assertEqual(len(ranked), NOW_ROOM_SLOTS)

    async def test_an_off_ac_room_can_fill_a_slot(self):
        self.room("a", "Majlis")
        self.room("b", "Bedroom")
        self.device("a", light("light.a"))
        self.device("b", ac("climate.b", "off"))
        ranked = await self.ranked()
        self.assertEqual([r["room_id"] for r in ranked], ["a", "b"])
        self.assertTrue(ranked[1]["rank"]["filler"])

    async def test_five_rooms_on_leave_no_slot_to_fill(self):
        for index in range(6):
            self.room(f"r{index}", f"Room {index}")
            self.device(f"r{index}", light(f"light.r{index}", "on" if index else "off"))
        ranked = await self.ranked()
        self.assertEqual([r["room_id"] for r in ranked], [f"r{i}" for i in range(1, 6)])
        self.assertFalse(any(r["rank"]["filler"] for r in ranked))

    async def test_a_home_with_policies_lists_every_participating_room(self):
        for index in range(7):
            self.room(f"p{index}", f"Room {index}", 7 - index)
            self.device(f"p{index}", light(f"light.p{index}", "off"))
            self.policies[f"p{index}"] = {
                "participates": True,
                "eligible_entity_ids": [f"light.p{index}"],
            }
        # A participating room with no approved device, and one that doesn't
        # take part (its light is on).
        self.room("none", "Store", -1)
        self.policies["none"] = {"participates": True, "eligible_entity_ids": []}
        self.room("out", "Out")
        self.device("out", light("light.out"))
        self.set("light.p3", "on")
        ranked = await self.ranked()
        # p3 is on; four fillers by sort_order (p6 has 1, p5 2, ...), then the
        # other participating rooms in the same order. Store sorts first but
        # never fills a slot: it has nothing to switch.
        self.assertEqual(
            [(r["room_id"], r["rank"]["filler"]) for r in ranked],
            [
                ("p3", False),
                ("p6", True),
                ("p5", True),
                ("p4", True),
                ("p2", True),
                ("none", False),
                ("p1", False),
                ("p0", False),
            ],
        )

    async def test_a_room_without_a_stored_policy_takes_part_with_its_lights(self):
        # Once one stored policy takes part, a room without one is listed
        # too, counting its lights, fans and the wall relays typed as one;
        # never its plug or untyped relay.
        self.room("a", "Majlis")
        self.room("b", "Bedroom")
        self.device("a", light("light.a"))
        self.device(
            "b",
            light("light.b"),
            relay("switch.b_plug", gang="outlet"),
            relay("switch.b_relay", gang="switch"),
            relay("switch.b_wall", gang="light"),
        )
        default = {"participates": True, "eligible_entity_ids": [], "default": True}
        self.policies = {
            "a": {"participates": True, "eligible_entity_ids": ["light.a"]},
            "b": default,
        }
        self.assertEqual(
            {
                r["room_id"]: (r["active_count"], r["eligible_entity_ids"])
                for r in await self.ranked()
            },
            {"a": (1, ["light.a"]), "b": (2, ["light.b", "switch.b_wall"])},
        )
        # Rooms that all take part by default rank as with no policy at all.
        self.policies = {"a": default, "b": default}
        self.assertEqual(
            {r["room_id"]: r["active_count"] for r in await self.ranked()},
            {"a": 1, "b": 4},
        )

    # -- ties -------------------------------------------------------------------

    async def test_count_ties_go_to_sort_order_then_name_then_room_id(self):
        self.room("z-late", "Alpha", 1)
        self.room("y-name", "Beta", 0)
        self.room("x-id2", "Alpha", 0)
        self.room("w-id1", "Alpha", 0)
        for room_id in ["z-late", "y-name", "x-id2", "w-id1"]:
            self.device(room_id, light(f"light.{room_id}"))
        self.assertEqual(await self.order(), ["w-id1", "x-id2", "y-name", "z-late"])

    async def test_watts_within_ten_percent_tie_and_go_to_count_then_order(self):
        self.room("a", "Alpha")
        self.room("b", "Beta")
        self.room("c", "Gamma")
        self.device("a", relay("switch.a"), power("sensor.a", "1845"))
        self.device(
            "b",
            relay("switch.b1"),
            relay("switch.b2"),
            power("sensor.b1", "1000"),
            power("sensor.b2", "840"),
        )
        self.device("c", relay("switch.c"), power("sensor.c", "1838"))
        self.assertEqual(await self.basis(), "watts")
        # 1845 W, 1840 W and 1838 W share a 10 % step, so raw watts don't
        # decide: b has two devices on, then a and c go by name.
        self.assertEqual(await self.order(), ["b", "a", "c"])

    async def test_watts_more_than_ten_percent_apart_beat_count(self):
        self.room("a", "Alpha")
        self.room("b", "Beta")
        self.device("a", relay("switch.a"), power("sensor.a", "300"))
        self.device(
            "b",
            relay("switch.b1"),
            relay("switch.b2"),
            power("sensor.b1", "130"),
            power("sensor.b2", "130"),
        )
        self.assertEqual(await self.order(), ["a", "b"])

    async def test_the_same_home_always_ranks_the_same(self):
        self.metered_home()
        self.device("kitchen", light("light.kitchen_bulb"))
        first = json.dumps(await self.ranked())
        self.assertEqual(json.dumps(await self.ranked()), first)
        # Nor does the order the registry lists rooms and devices in matter.
        self.rooms.reverse()
        self.devices.reverse()
        self.assertEqual(json.dumps(await self.ranked()), first)

    # -- scope ------------------------------------------------------------------

    async def test_the_basis_and_order_only_look_at_the_callers_rooms(self):
        self.metered_home()
        self.room("office", "Office")
        self.device("office", light("light.office"))
        self.assertEqual(await self.basis(), "count")
        scope = ["majlis", "bedroom", "kitchen"]
        self.assertEqual(await self.basis(scope), "watts")
        scoped = json.dumps(await self.ranked(scope))
        # Changing only a room outside the scope changes nothing in it.
        self.set("light.office", "off")
        self.assertEqual(json.dumps(await self.ranked(scope)), scoped)
        self.set("sensor.majlis_ac", "unavailable")
        self.assertNotEqual(json.dumps(await self.ranked(scope)), scoped)


class GeneratedFollowsTheRankingTest(unittest.IsolatedAsyncioTestCase):
    """The generated room suggestions pick their rooms from the same ranking."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = fixtures.storage.HubStorage(Path(tmp.name) / "hub.db")
        db.open()
        self.addCleanup(db.close)

        def state(eid, value, **attributes):
            return NS(entity_id=eid, state=value, attributes=attributes)

        self.values = {
            # Living: one 300 W light (a metered relay). Kitchen: two lights,
            # 40 W. Bedroom: only its AC, 1.5 kW.
            "light.living": state("light.living", "on"),
            "sensor.living": state(
                "sensor.living", "300", device_class="power", unit_of_measurement="W"
            ),
            "light.kitchen_1": state("light.kitchen_1", "on"),
            "light.kitchen_2": state("light.kitchen_2", "on"),
            "sensor.kitchen": state(
                "sensor.kitchen", "40", device_class="power", unit_of_measurement="W"
            ),
            "climate.bedroom": state("climate.bedroom", "cool", hvac_modes=AC_MODES),
            "sensor.bedroom": state(
                "sensor.bedroom",
                "1.5",
                device_class="power",
                unit_of_measurement="kW",
            ),
        }
        areas = {eid: eid.split(".")[1].split("_")[0] for eid in self.values}

        async def executor(fn, *args):
            return fn(*args)

        values = self.values
        hass = NS(
            states=NS(values=values, async_all=lambda: list(values.values())),
            rooms=areas,
            config=NS(time_zone="UTC"),
            async_add_executor_job=executor,
        )
        registry = NS(
            list_rooms=lambda: [
                {"room_id": "living", "name": "Living"},
                {"room_id": "kitchen", "name": "Kitchen"},
                {"room_id": "bedroom", "name": "Bedroom"},
            ],
            list_user_devices=lambda: [
                {"control_entity_ids": ["light.living", "sensor.living"]},
                {
                    "control_entity_ids": [
                        "light.kitchen_1",
                        "light.kitchen_2",
                        "sensor.kitchen",
                    ]
                },
                {"control_entity_ids": ["climate.bedroom", "sensor.bedroom"]},
            ],
        )
        self.runtime = RUNTIME.GeneratedSuggestionRuntime(
            hass,
            fixtures.stores.SuggestionStore(db),
            registry,
            NS(room_policy=lambda rid: {}),
            clock=lambda: datetime(2026, 10, 23, 18, 0, tzinfo=UTC),
        )

    async def test_watts_order_picks_the_first_room_and_skips_ac_only_rooms(self):
        ranked, _, _ = await self.runtime.room_context(None)
        self.assertEqual(
            [r["room_id"] for r in ranked], ["bedroom", "living", "kitchen"]
        )
        self.assertEqual(ranking_summary(ranked)["basis"], "watts")
        data, *_ = await self.runtime.context(None)
        # Bedroom leads the Now page, but only rooms with a light, fan or
        # switch on are chosen (active_count > 0): Living (300 W) first,
        # although Kitchen has more devices on.
        self.assertEqual(
            [(p["room_id"], p["kind"]) for p in data["_generated_plans"]],
            [("living", "room_off"), ("kitchen", "room_off")],
        )


if __name__ == "__main__":
    unittest.main()
