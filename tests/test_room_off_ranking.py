"""The all-off room ranking: still running, just went dark, room order.

Runs without Home Assistant, on the suggestion-runtime fixture the on-ranking tests
use: the real room_intel.ranking and room_intel.off_rooms over fake states, a
fake registry, the fixture's filtering (served = has a state, room = the
fixture's room map) and a fake room activity log that answers like
ActivityRecorder.async_recent_events (that one is tested against a real Home
Assistant in test_room_off_ranking_ha).
"""

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import test_suggestion_api as api_fixture
from test_room_on_ranking import ac, light, power, relay

RUNTIME = api_fixture.RUNTIME
_RANKING = RUNTIME.async_rank_rooms.__globals__
async_rank_rooms = _RANKING["async_rank_rooms"]
order_off_rooms = _RANKING["order_off_rooms"]
ranking_summary = _RANKING["ranking_summary"]
OffRoomFacts = _RANKING["OffRoomFacts"]

NOW = 1_800_000_000
MIN = 60


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


def media(eid, state="playing", **attributes):
    return eid, state, dict(attributes)


def cover(eid, state="open"):
    return eid, state, {"current_position": 100}


class Log:
    """The room activity log as the ranking reads it (the recorder)."""

    def __init__(self):
        self.events = []
        self.started_at = None
        self.reads = []

    def add(self, room_id, entity_id, action, ago, source="wall_switch", member=None):
        self.events.append(
            {
                "t": NOW - ago,
                "utc_offset_min": 180,
                "room_id": room_id,
                "entity_id": entity_id,
                "action": action,
                "source": source,
                "member_id": member,
            }
        )

    async def async_recent_events(self, since_t, room_ids):
        self.reads.append((since_t, list(room_ids)))
        rows = [
            (index, event)
            for index, event in enumerate(self.events)
            if event["room_id"] in room_ids
            and event["t"] >= since_t
            and event["action"] in ("on", "off", "set")
        ]
        rows.sort(key=lambda item: (-item[1]["t"], -item[0]))
        return [dict(event) for _, event in rows]


class OffRankingCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rooms = []
        self.values = {}
        self.areas = {}
        self.devices = []
        self.policies = {}
        self.log = Log()

    def room(self, room_id, name=None, sort_order=None):
        self.rooms.append(
            {
                "room_id": room_id,
                "name": name or room_id.title(),
                "sort_order": len(self.rooms) if sort_order is None else sort_order,
            }
        )

    def device(self, room_id, *entities):
        """One registry device in a room: (entity_id, state, attributes[, gang])."""
        record = {"control_entity_ids": [], "config_entity_ids": [], "gangs": {}}
        for entity in entities:
            eid, state, attributes = entity[:3]
            self.values[eid] = NS(
                entity_id=eid, state=state, attributes=dict(attributes)
            )
            self.areas[eid] = room_id
            record["control_entity_ids"].append(eid)
            if len(entity) == 4:
                record["gangs"][eid] = {"type": entity[3]}
        self.devices.append(record)
        return record

    def home(self):
        """Six rooms in this order, each with one light, everything off."""
        for room_id in ("majlis", "kitchen", "bedroom", "office", "guest", "hall"):
            self.room(room_id)
            self.device(room_id, light(f"light.{room_id}", "off"))

    def participate_all(self):
        """Every room takes part in room commands, with all its safe devices."""
        for room in self.rooms:
            rid = room["room_id"]
            self.policies[rid] = {
                "participates": True,
                "eligible_entity_ids": [
                    eid
                    for eid, area in self.areas.items()
                    if area == rid and eid.split(".")[0] in ("light", "switch", "fan")
                ],
            }

    def unavailable(self, eid, since_ago=None, state="unavailable"):
        """The device is unknown/unavailable; since_ago None: change time unknown."""
        value = self.values[eid]
        value.state = state
        if since_ago is not None:
            value.last_changed_timestamp = float(NOW - since_ago)

    async def ranked(self, scope=None, *, member=None, now=NOW, log=True):
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
        ranked, _, _ = await async_rank_rooms(
            hass,
            registry,
            now_data,
            scope,
            activity=self.log if log else None,
            member_id=member,
            now_t=now,
        )
        return ranked

    async def order(self, scope=None, **kwargs):
        return [
            (r["room_id"], r["rank"]["reason"])
            for r in await self.ranked(scope, **kwargs)
        ]

    async def reasons(self, scope=None, **kwargs):
        return {
            r["room_id"]: r["rank"]["reason"]
            for r in await self.ranked(scope, **kwargs)
        }


class WholeHouseTest(OffRankingCase):
    async def test_a_whole_house_off_ranks_running_then_just_off_then_room_order(self):
        self.home()
        self.device("majlis", media("media_player.majlis_tv"))
        self.log.add("kitchen", "light.kitchen", "on", 40 * MIN)
        self.log.add("kitchen", "light.kitchen", "off", 5 * MIN)
        self.log.add("bedroom", "light.bedroom", "off", 20 * MIN, source="ha_user")
        self.log.add("office", "light.office", "off", 2 * 86400)  # long ago
        ranked = await self.ranked()
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["filler"]) for r in ranked],
            [
                ("majlis", "running_load", True),
                ("kitchen", "just_off", True),
                ("bedroom", "just_off", True),
                ("office", "room_order", True),
                ("guest", "room_order", True),
            ],
        )
        self.assertEqual(
            [r["rank"]["params"] for r in ranked],
            [
                {"loads": ["media"]},
                {"went_dark_at": iso(NOW - 5 * MIN)},
                {"went_dark_at": iso(NOW - 20 * MIN)},
                {},
                {},
            ],
        )
        self.assertEqual(ranking_summary(ranked)["mode"], "all_off")
        # rank keeps its first fields; reason and params come last.
        self.assertEqual(
            list(ranked[0]["rank"]),
            [
                "active",
                "filler",
                "on_count",
                "ac_on_count",
                "watts",
                "watts_quality",
                "reason",
                "params",
            ],
        )

    async def test_on_rooms_come_first_and_the_off_rooms_fill_by_tier(self):
        self.home()
        self.values["light.guest"].state = "on"
        self.device("hall", ac("climate.hall"))  # AC-only on
        self.device("majlis", media("media_player.majlis_tv"))
        self.log.add("bedroom", "light.bedroom", "off", 3 * MIN)
        self.log.add("kitchen", "light.kitchen", "off", 9 * MIN)
        ranked = await self.ranked()
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["filler"]) for r in ranked],
            [
                ("guest", "active", False),
                ("hall", "active", False),
                ("majlis", "running_load", True),
                ("bedroom", "just_off", True),
                ("kitchen", "just_off", True),
            ],
        )
        self.assertEqual(ranking_summary(ranked)["mode"], "on")

    async def test_a_home_with_policies_lists_every_off_room_in_tier_order(self):
        self.home()
        self.participate_all()
        self.values["light.majlis"].state = "on"
        self.log.add("hall", "light.hall", "off", 1 * MIN)
        self.log.add("office", "light.office", "off", 4 * MIN)
        ranked = await self.ranked()
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["filler"]) for r in ranked],
            [
                ("majlis", "active", False),
                ("hall", "just_off", True),
                ("office", "just_off", True),
                ("kitchen", "room_order", True),
                ("bedroom", "room_order", True),
                # Past the five slots, still listed for room commands.
                ("guest", "room_order", False),
            ],
        )

    async def test_without_the_log_no_room_is_just_off(self):
        # The generated suggestions rank without the log: they only use rooms
        # that are on.
        self.home()
        self.log.add("kitchen", "light.kitchen", "off", 1 * MIN)
        self.assertEqual(
            await self.order(log=False),
            [
                ("majlis", "room_order"),
                ("kitchen", "room_order"),
                ("bedroom", "room_order"),
                ("office", "room_order"),
                ("guest", "room_order"),
            ],
        )
        self.assertEqual(self.log.reads, [])


class StillRunningTest(OffRankingCase):
    def running_home(self):
        """Rooms in this order; each has a light (off) and one more thing."""
        cases = [
            # Running.
            ("heater", [relay("switch.h_light", "off"), relay("switch.h_boiler", "on", "heater"), power("sensor.h_power", "1500")]),
            ("cinema", [light("light.cinema", "off"), media("media_player.cinema")]),
            ("lounge", [relay("switch.l_light", "off"), power("sensor.l_power", "60"), media("media_player.lounge")]),
            ("edge", [relay("switch.e_light", "off"), power("sensor.e_power", "20")]),
            # Truly idle.
            ("zero", [relay("switch.z_light", "off"), power("sensor.z_power", "0")]),
            ("standby", [relay("switch.s_light", "off"), power("sensor.s_power", "19.9")]),
            ("router", [relay("switch.r_light", "off"), power("sensor.r_power", "8")]),
            ("paused", [light("light.paused", "off"), media("media_player.paused", "paused")]),
            ("idle_tv", [light("light.idle_tv", "off"), media("media_player.idle_tv", "idle")]),
            ("on_tv", [light("light.on_tv", "off"), media("media_player.on_tv", "on")]),
            ("curtain", [light("light.curtain", "off"), cover("cover.curtain", "open")]),
            ("moving", [light("light.moving", "off"), cover("cover.moving", "opening")]),
            ("boiler", [light("light.boiler", "off"), relay("switch.boiler", "on", "heater")]),
            ("excluded", [light("light.excluded", "off"), media("media_player.excluded", casasmart_room_activity_exclude=True)]),
            ("dead_meter", [relay("switch.d_light", "off"), power("sensor.d_power", "unavailable")]),
            ("dead_tv", [light("light.dead_tv", "off"), media("media_player.dead_tv", "unavailable")]),
        ]  # fmt: skip
        for room_id, entities in cases:
            self.room(room_id)
            self.device(room_id, *entities)
        # Fifteen meters idling at 1.5 W each (22.5 W together): no load.
        self.room("many")
        for index in range(15):
            self.device(
                "many",
                relay(f"switch.m_{index}", "off"),
                power(f"sensor.m_{index}", "1.5"),
            )
        self.participate_all()

    async def test_media_playing_or_a_meter_drawing_20_w_is_still_running(self):
        self.running_home()
        ranked = await self.ranked()
        running = [
            (r["room_id"], r["rank"]["params"]["loads"], r["rank"]["watts"])
            for r in ranked
            if r["rank"]["reason"] == "running_load"
        ]
        # Higher watts first; a room without a reading after.
        self.assertEqual(
            running,
            [
                ("heater", ["power"], 1500.0),
                ("lounge", ["power", "media"], 60.0),
                ("edge", ["power"], 20.0),
                ("cinema", ["media"], None),
            ],
        )
        self.assertEqual(
            [r["room_id"] for r in ranked][4:],
            [
                "zero",
                "standby",
                "router",
                "paused",
                "idle_tv",
                "on_tv",
                "curtain",
                "moving",
                "boiler",
                "excluded",
                "dead_meter",
                "dead_tv",
                "many",
            ],
        )
        self.assertTrue(
            all(r["rank"]["reason"] == "room_order" for r in ranked[4:]), ranked
        )
        self.assertTrue(all(not r["rank"]["active"] for r in ranked))
        self.assertEqual(ranked[-1]["rank"]["watts"], 22.5)

    async def test_a_room_with_only_a_playing_tv_fills_a_slot(self):
        self.room("majlis")
        self.device("majlis", media("media_player.majlis_tv"))
        self.room("study")
        self.device("study", media("media_player.study_tv", "paused"))
        self.assertEqual(await self.order(), [("majlis", "running_load")])

    async def test_running_beats_just_off(self):
        self.home()
        self.device("hall", media("media_player.hall_tv"))
        self.log.add("kitchen", "light.kitchen", "off", 10)
        self.assertEqual(
            (await self.order())[:2],
            [("hall", "running_load"), ("kitchen", "just_off")],
        )


class JustOffTest(OffRankingCase):
    async def test_newest_first_within_30_minutes(self):
        self.home()
        self.log.add("kitchen", "light.kitchen", "off", 29 * MIN)
        self.log.add("bedroom", "light.bedroom", "off", 2 * MIN)
        self.log.add("office", "light.office", "off", 30 * MIN)  # on the edge
        self.log.add("guest", "light.guest", "off", 30 * MIN + 1)
        self.assertEqual(
            await self.order(),
            [
                ("bedroom", "just_off"),
                ("kitchen", "just_off"),
                ("office", "just_off"),
                ("majlis", "room_order"),
                ("guest", "room_order"),
            ],
        )
        # The log is read for the scoped rooms and the last 30 minutes only.
        self.assertEqual(
            self.log.reads[-1],
            (NOW - 1800, ["majlis", "kitchen", "bedroom", "office", "guest", "hall"]),
        )

    async def test_a_room_drops_out_after_30_minutes(self):
        self.home()
        self.log.add("kitchen", "light.kitchen", "off", 29 * MIN)
        self.log.add("bedroom", "light.bedroom", "off", 2 * MIN)
        self.assertEqual(
            await self.order(now=NOW + 61),
            [
                ("bedroom", "just_off"),
                ("majlis", "room_order"),
                ("kitchen", "room_order"),
                ("office", "room_order"),
                ("guest", "room_order"),
            ],
        )

    async def test_a_tie_goes_to_the_room_order(self):
        self.home()
        self.log.add("office", "light.office", "off", 3 * MIN)
        self.log.add("kitchen", "light.kitchen", "off", 3 * MIN)
        self.assertEqual(
            (await self.order())[:2],
            [("kitchen", "just_off"), ("office", "just_off")],
        )

    async def test_only_offs_a_person_made_count_and_the_viewers_own_sort_last(self):
        self.home()
        self.participate_all()
        self.log.add("kitchen", "light.kitchen", "off", 1 * MIN, "automation")
        self.log.add("bedroom", "light.bedroom", "off", 1 * MIN, "scene", "mem-1")
        self.log.add("office", "light.office", "off", 1 * MIN, "app", "mem-1")
        self.log.add("guest", "light.guest", "off", 1 * MIN, "app", "mem-2")
        self.log.add("hall", "light.hall", "off", 1 * MIN, "unknown")
        self.log.add("majlis", "light.majlis", "off", 1 * MIN, "ha_user")
        just_off = {"majlis", "guest", "hall"}
        for member, own in (("mem-1", "office"), ("mem-2", "guest"), (None, None)):
            with self.subTest(member=member):
                ranked = await self.ranked(member=member)
                theirs = {
                    r["room_id"]
                    for r in ranked
                    if r["rank"]["reason"] == "just_off"
                    and "by_you" not in r["rank"]["params"]
                }
                self.assertEqual(theirs, (just_off | {"office", "guest"}) - {own})
                # Own-tap demotion: the room the viewer switched
                # off from the app is still just_off, marked by_you, after
                # everyone else's and before the room order.
                order = [r["room_id"] for r in ranked]
                mine = [r for r in ranked if r["rank"]["params"].get("by_you")]
                self.assertEqual([r["room_id"] for r in mine], [own] if own else [])
                if own:
                    self.assertEqual(mine[0]["rank"]["reason"], "just_off")
                    self.assertEqual(
                        mine[0]["rank"]["params"],
                        {"went_dark_at": iso(NOW - MIN), "by_you": True},
                    )
                    self.assertEqual(order.index(own), len(theirs))
                # No member (or entity) id ever goes into a reason.
                ranked = json.dumps([r["rank"] for r in ranked])
                self.assertNotIn("mem-", ranked)
                self.assertNotIn("light.", ranked)

    async def test_wall_switch_off_after_an_automation_on_counts(self):
        self.home()
        self.log.add("kitchen", "light.kitchen", "on", 6 * MIN, "automation")
        self.log.add("kitchen", "light.kitchen", "off", 5 * MIN)
        self.assertEqual((await self.order())[0], ("kitchen", "just_off"))

    async def test_the_room_goes_dark_with_its_last_device(self):
        self.home()
        self.device("kitchen", light("light.kitchen_2", "off"))
        self.log.add("kitchen", "light.kitchen", "off", 12 * MIN)
        self.log.add("kitchen", "light.kitchen_2", "off", 4 * MIN)
        ranked = await self.ranked()
        self.assertEqual(
            ranked[0]["rank"]["params"], {"went_dark_at": iso(NOW - 4 * MIN)}
        )

    async def test_the_newest_event_of_a_counted_device_must_be_an_off(self):
        self.home()
        # Kitchen: on again after its off, then dark through unavailable
        # (logged nothing).
        self.log.add("kitchen", "light.kitchen", "off", 10 * MIN)
        self.log.add("kitchen", "light.kitchen", "on", 5 * MIN)
        # Bedroom: its AC changed mode, then went off unlogged.
        self.device("bedroom", ac("climate.bedroom", "off"))
        self.log.add("bedroom", "climate.bedroom", "on", 20 * MIN)
        self.log.add("bedroom", "light.bedroom", "off", 10 * MIN)
        self.log.add("bedroom", "climate.bedroom", "set", 5 * MIN)
        # Office: a TV switched off after the light is not a counted device.
        self.device("office", media("media_player.office_tv", "off"))
        self.log.add("office", "light.office", "off", 10 * MIN)
        self.log.add("office", "media_player.office_tv", "off", 2 * MIN)
        # Hall: a heater gang switched on after the light went off.
        self.device("hall", relay("switch.hall_boiler", "on", "heater"))
        self.log.add("hall", "light.hall", "off", 8 * MIN)
        self.log.add("hall", "switch.hall_boiler", "on", 1 * MIN)
        # Guest: its AC is what went off.
        self.device("guest", ac("climate.guest", "off"))
        self.log.add("guest", "climate.guest", "off", 15 * MIN)
        self.participate_all()
        ranked = await self.ranked()
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"], r["rank"]["params"]) for r in ranked],
            [
                ("hall", "just_off", {"went_dark_at": iso(NOW - 8 * MIN)}),
                ("office", "just_off", {"went_dark_at": iso(NOW - 10 * MIN)}),
                ("guest", "just_off", {"went_dark_at": iso(NOW - 15 * MIN)}),
                ("majlis", "room_order", {}),
                ("kitchen", "room_order", {}),
                ("bedroom", "room_order", {}),
            ],
        )

    async def test_a_clock_that_went_backwards_makes_nothing_just_off(self):
        self.home()
        self.log.add("kitchen", "light.kitchen", "off", -10 * MIN)
        self.log.add("bedroom", "light.bedroom", "off", -30)  # within 60 s
        reasons = await self.reasons()
        self.assertEqual(
            (reasons["kitchen"], reasons["bedroom"]), ("room_order", "just_off")
        )


class UnavailableTest(OffRankingCase):
    async def test_unavailable_and_unknown_never_make_a_room_just_off(self):
        self.home()
        for rid in ("kitchen", "bedroom", "office", "guest"):
            self.device(rid, light(f"light.{rid}_2", "off"))
            self.log.add(rid, f"light.{rid}", "off", 10 * MIN)
        # Kitchen: its other light dropped off the network after the off,
        # so the room may only look dark.
        self.unavailable("light.kitchen_2", since_ago=5 * MIN)
        # Bedroom: that light has been dead since long before.
        self.unavailable("light.bedroom_2", since_ago=2 * 3600)
        # Office: unknown, and since when isn't known.
        self.unavailable("light.office_2", state="unknown")
        # Guest: unknown since an hour ago.
        self.unavailable("light.guest_2", since_ago=3600, state="unknown")
        # Hall: its light went on, then unavailable (nothing logged after).
        self.log.add("hall", "light.hall", "on", 20 * MIN)
        self.unavailable("light.hall", since_ago=3 * MIN)
        self.participate_all()
        self.assertEqual(
            await self.reasons(),
            {
                "bedroom": "just_off",
                "guest": "just_off",
                "majlis": "room_order",
                "kitchen": "room_order",
                "office": "room_order",
                "hall": "room_order",
            },
        )

    async def test_an_unavailable_ac_blocks_like_a_light(self):
        self.home()
        self.device("kitchen", ac("climate.kitchen", "cool"))
        self.log.add("kitchen", "light.kitchen", "off", 10 * MIN)
        self.unavailable("climate.kitchen", since_ago=1 * MIN)
        self.assertEqual((await self.reasons())["kitchen"], "room_order")


class RestartTest(OffRankingCase):
    async def test_offs_in_the_first_ten_minutes_after_start_dont_count(self):
        self.home()
        self.log.started_at = NOW - 5 * MIN
        # Logged after the start: a device catching up with what happened
        # while Home Assistant was down.
        self.log.add("kitchen", "light.kitchen", "off", 2 * MIN)
        # Logged before the restart: real.
        self.log.add("bedroom", "light.bedroom", "off", 7 * MIN)
        reasons = await self.reasons()
        self.assertEqual(
            (reasons["kitchen"], reasons["bedroom"]), ("room_order", "just_off")
        )

    async def test_after_ten_minutes_offs_count_again(self):
        self.home()
        self.log.started_at = NOW - 11 * MIN
        self.log.add("kitchen", "light.kitchen", "off", 2 * MIN)  # 9 min in
        self.log.add("bedroom", "light.bedroom", "off", 30)  # 10.5 min in
        reasons = await self.reasons()
        self.assertEqual(
            (reasons["kitchen"], reasons["bedroom"]), ("room_order", "just_off")
        )

    async def test_an_off_in_the_same_second_as_the_start_is_quiet_too(self):
        # Event times are whole seconds; the start time isn't.
        self.home()
        self.log.started_at = NOW - 3 * MIN + 0.7
        self.log.add("kitchen", "light.kitchen", "off", 3 * MIN)
        self.log.add("bedroom", "light.bedroom", "off", 3 * MIN + 1)  # before
        reasons = await self.reasons()
        self.assertEqual(
            (reasons["kitchen"], reasons["bedroom"]), ("room_order", "just_off")
        )

    async def test_a_reload_has_no_quiet_window(self):
        self.home()
        self.log.started_at = None
        self.log.add("kitchen", "light.kitchen", "off", 10)
        self.assertEqual((await self.reasons())["kitchen"], "just_off")


class RoomsTest(OffRankingCase):
    async def test_a_moved_device_takes_no_history_with_it(self):
        self.home()
        self.participate_all()
        self.room("living")
        self.device("living", light("light.living", "off"))
        # The lamp went off in the Kitchen, then was moved to the Living room.
        self.device("kitchen", light("light.lamp", "off"))
        self.log.add("kitchen", "light.lamp", "off", 5 * MIN)
        self.areas["light.lamp"] = "living"
        self.participate_all()
        reasons = await self.reasons()
        self.assertEqual(
            (reasons["kitchen"], reasons["living"]), ("room_order", "room_order")
        )
        # Its next off in the Living room counts there.
        self.log.add("living", "light.lamp", "off", 1 * MIN)
        reasons = await self.reasons()
        self.assertEqual(
            (reasons["kitchen"], reasons["living"]), ("room_order", "just_off")
        )

    async def test_a_deleted_room_is_never_read_or_listed(self):
        self.home()
        self.log.add("gone", "light.gone", "off", 1 * MIN)
        ranked = await self.ranked()
        self.assertNotIn("gone", [r["room_id"] for r in ranked])
        self.assertNotIn("gone", self.log.reads[-1][1])


class ScopeTest(OffRankingCase):
    async def test_hidden_rooms_never_change_a_scoped_members_rooms(self):
        self.home()
        scope = ["kitchen", "office"]
        self.log.add("kitchen", "light.kitchen", "off", 3 * MIN)
        before = json.dumps(await self.ranked(scope, member="mem-1"))
        # Everything changes in the rooms the member can't see.
        self.device("majlis", media("media_player.majlis_tv"))
        self.device("bedroom", relay("switch.bed", "off"), power("sensor.bed", "2000"))
        self.log.add("bedroom", "light.bedroom", "off", 1 * MIN)
        self.log.add("guest", "light.guest", "off", 2 * MIN, "app", "mem-2")
        self.values["light.hall"].state = "on"
        self.unavailable("light.guest", since_ago=1)
        after = await self.ranked(scope, member="mem-1")
        self.assertEqual(json.dumps(after), before)
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"]) for r in after],
            [("kitchen", "just_off"), ("office", "room_order")],
        )
        self.assertEqual(ranking_summary(after)["mode"], "all_off")
        # The log is only read for the member's rooms.
        self.assertEqual({tuple(rooms) for _, rooms in self.log.reads}, {tuple(scope)})


class LearnedHookTest(unittest.TestCase):
    """The learned hook: a tier between just_off and room_order."""

    def test_learned_rooms_sort_between_just_off_and_room_order(self):
        rooms = [
            {"room_id": rid, "name": rid, "sort_order": index, "rank": {}}
            for index, rid in enumerate("abcde")
        ]
        facts = {
            "a": OffRoomFacts([], None, None),
            "b": OffRoomFacts([], None, NOW),
            "c": OffRoomFacts(["media"], None, NOW),
            "d": OffRoomFacts([], None, None),
            "e": OffRoomFacts([], None, None),
        }
        asked = []

        def learned(room):
            asked.append(room["room_id"])
            return {
                "a": ("habit_now", {}, (0, -0.4)),
                "e": ("habit_now", {}, (0, -0.9)),
            }.get(room["room_id"])

        ordered = order_off_rooms(rooms, facts, learned)
        self.assertEqual(
            [(r["room_id"], r["rank"]["reason"]) for r in ordered],
            [
                ("c", "running_load"),
                ("b", "just_off"),
                ("e", "habit_now"),
                ("a", "habit_now"),
                ("d", "room_order"),
            ],
        )
        # Rooms already in a better tier aren't asked.
        self.assertEqual(sorted(asked), ["a", "d", "e"])
        # Without the hook, those rooms are in the room order.
        ordered = order_off_rooms(rooms, facts)
        self.assertEqual([r["room_id"] for r in ordered], ["c", "b", "a", "d", "e"])


if __name__ == "__main__":
    unittest.main()
