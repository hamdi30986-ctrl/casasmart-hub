"""Focused contract tests for the Hub-owned Now data foundation."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "casasmart" / "now_data.py"
)
_SPEC = importlib.util.spec_from_file_location("casasmart_now_data", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_NOW = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_NOW)


class Table(dict):
    """In-memory KeyValueTable equivalent for pure engine tests."""


def make_engine():
    return _NOW.NowDataEngine(Table(), Table(), Table(), Table(), Table())


AC_ATTRIBUTES = {
    "hvac_modes": ["off", "cool", "dry"],
    "temperature": 22,
    "min_temp": 16,
    "max_temp": 30,
    "supported_features": 9,
    "fan_mode": "high",
    "fan_modes": ["low", "high"],
}


def ac_state(state="cool", **attributes):
    return {
        "entity_id": "climate.ac",
        "state": state,
        "attributes": {**AC_ATTRIBUTES, **attributes},
    }


class AcCaptureTest(unittest.TestCase):
    def test_room_off_keeps_a_running_acs_mode_and_target(self) -> None:
        self.assertEqual(
            _NOW.ac_capture_level(ac_state()),
            {"hvac_mode": "cool", "temperature": 22},
        )

    def test_an_ac_with_no_target_keeps_its_mode_alone(self) -> None:
        for attributes in ({"temperature": None}, {"supported_features": 8}):
            with self.subTest(attributes=attributes):
                self.assertEqual(
                    _NOW.ac_capture_level(ac_state("dry", **attributes)),
                    {"hvac_mode": "dry"},
                )

    def test_only_a_running_ac_is_captured(self) -> None:
        for state in (
            ac_state("off"),
            ac_state("unavailable"),
            ac_state(casasmart_room_activity_exclude=True),
            ac_state(hvac_modes=["off", "heat"]),
            {"entity_id": "light.a", "state": "on", "attributes": {}},
        ):
            with self.subTest(state=state):
                self.assertIsNone(_NOW.ac_capture_level(state))

    def test_it_comes_back_by_mode_then_target_and_no_fan_step(self) -> None:
        level = {"hvac_mode": "cool", "temperature": 22, "fan_mode": "high"}
        self.assertEqual(
            _NOW.ac_restore_steps(ac_state("off"), level),
            [
                {
                    "entity_id": "climate.ac",
                    "action": "set_hvac_mode",
                    "data": {"hvac_mode": "cool"},
                },
                {
                    "entity_id": "climate.ac",
                    "action": "set_temperature",
                    "data": {"temperature": 22},
                },
            ],
        )
        self.assertEqual(
            _NOW.ac_restore_steps(ac_state("off"), {"hvac_mode": "dry"}),
            [
                {
                    "entity_id": "climate.ac",
                    "action": "set_hvac_mode",
                    "data": {"hvac_mode": "dry"},
                },
            ],
        )

    def test_it_never_comes_back_at_a_guess(self) -> None:
        for state, level in (
            (ac_state("off"), {}),
            (ac_state("off"), {"hvac_mode": "off"}),
            # A mode or target it no longer takes.
            (ac_state("off"), {"hvac_mode": "heat", "temperature": 22}),
            (ac_state("off"), {"hvac_mode": "cool", "temperature": 12}),
            (ac_state("off"), {"hvac_mode": "cool", "temperature": 31}),
            (ac_state("off"), {"hvac_mode": "cool", "temperature": "22"}),
            (
                ac_state("off", supported_features=8),
                {"hvac_mode": "cool", "temperature": 22},
            ),
            # Left out of room activity since.
            (
                ac_state("off", casasmart_room_activity_exclude=True),
                {"hvac_mode": "cool", "temperature": 22},
            ),
        ):
            with self.subTest(state=state, level=level):
                self.assertEqual(_NOW.ac_restore_steps(state, level), [])


class RestoreAcsTest(unittest.TestCase):
    ROOM = "room-majlis"
    COOL = {"hvac_mode": "cool", "temperature": 22}
    AT = datetime(2026, 8, 24, 8, tzinfo=UTC)

    def extend(self, engine, ids, acs=None, seconds=0):
        return engine.extend_restore_set(
            self.ROOM, ids, self.AT + timedelta(seconds=seconds), acs
        )

    def test_a_capture_with_only_an_ac_is_a_capture(self) -> None:
        engine = make_engine()
        self.assertEqual(
            self.extend(engine, [], {"climate.ac": self.COOL}), ["climate.ac"]
        )
        self.assertEqual(engine.restore_set(self.ROOM), [])
        self.assertEqual(engine.restore_acs(self.ROOM), {"climate.ac": self.COOL})
        self.assertEqual(engine.restore_capture(self.ROOM)[1], self.AT.isoformat())

    def test_a_second_off_adds_and_keeps_when_it_was_captured(self) -> None:
        engine = make_engine()
        self.extend(engine, ["light.a"], {"climate.ac": self.COOL})
        # Finding nothing on leaves it as it is.
        self.assertEqual(self.extend(engine, [], seconds=30), ["light.a", "climate.ac"])
        dry = {"hvac_mode": "dry"}
        self.assertEqual(
            self.extend(
                engine, ["light.b"], {"climate.ac": dry, "climate.b": self.COOL}, 60
            ),
            ["light.a", "light.b", "climate.ac", "climate.b"],
        )
        self.assertEqual(
            engine.restore_acs(self.ROOM),
            {"climate.ac": dry, "climate.b": self.COOL},
        )
        self.assertEqual(engine.restore_capture(self.ROOM)[1], self.AT.isoformat())

    def test_a_second_off_finds_an_ac_only_capture_too(self) -> None:
        engine = make_engine()
        self.extend(engine, [], {"climate.ac": self.COOL})
        self.assertEqual(
            self.extend(engine, ["light.a"], seconds=60), ["light.a", "climate.ac"]
        )
        self.assertEqual(engine.restore_capture(self.ROOM)[1], self.AT.isoformat())

    def test_a_later_off_replaces_the_acs_too(self) -> None:
        engine = make_engine()
        self.extend(engine, ["light.a"], {"climate.ac": self.COOL})
        self.assertEqual(self.extend(engine, ["light.b"], seconds=600), ["light.b"])
        self.assertEqual(engine.restore_acs(self.ROOM), {})

    def test_what_didnt_come_back_stays_captured(self) -> None:
        engine = make_engine()
        self.extend(engine, ["light.a"], {"climate.ac": self.COOL, "climate.b": {}})
        self.assertEqual(
            engine.keep_in_restore_set(self.ROOM, ["climate.ac"]), ["climate.ac"]
        )
        self.assertEqual(engine.restore_set(self.ROOM), [])
        self.assertEqual(engine.restore_acs(self.ROOM), {"climate.ac": self.COOL})
        self.assertEqual(engine.restore_capture(self.ROOM)[1], self.AT.isoformat())
        self.assertEqual(engine.keep_in_restore_set(self.ROOM, []), [])
        self.assertEqual(engine.restore_acs(self.ROOM), {})

    def test_a_consumed_or_forgotten_capture_takes_its_acs(self) -> None:
        engine = make_engine()
        self.extend(engine, [], {"climate.ac": self.COOL})
        engine.consume_restore_set(self.ROOM)
        self.assertEqual(engine.restore_acs(self.ROOM), {})
        self.extend(engine, [], {"climate.ac": self.COOL})
        engine.forget_room(self.ROOM)
        self.assertEqual(engine.restore_acs(self.ROOM), {})

    def test_a_capture_from_an_older_hub_has_no_acs(self) -> None:
        engine = make_engine()
        engine.save_restore_set(self.ROOM, ["light.a"])
        self.assertEqual(engine.restore_acs(self.ROOM), {})
        for record in ("junk", {"acs": "junk"}, {"acs": {"climate.ac": "junk"}}):
            with self.subTest(record=record):
                engine._restores[self.ROOM] = record
                self.assertEqual(engine.restore_acs(self.ROOM), {})


class NowDataEngineTest(unittest.TestCase):
    def test_successful_recency_is_user_scoped_and_deduplicated(self) -> None:
        engine = make_engine()
        early = datetime(2026, 8, 24, 8, tzinfo=UTC)
        late = datetime(2026, 8, 24, 9, tzinfo=UTC)
        engine.record_successful_control("member-a", "light.kitchen", early)
        engine.record_successful_control("member-a", "switch.coffee", late)
        engine.record_successful_control("member-a", "light.kitchen", late)

        self.assertEqual(
            engine.recents_for("member-a"),
            [
                {"entity_id": "light.kitchen", "at": late.isoformat()},
                {"entity_id": "switch.coffee", "at": late.isoformat()},
            ],
        )
        self.assertEqual(engine.recents_for("member-b"), [])

    def test_malformed_stored_room_policy_is_ignored(self) -> None:
        engine = make_engine()
        for record in (["light.kitchen"], "participates", 1):
            with self.subTest(record=record):
                engine._policies["room-kitchen"] = record
                # As if there were none: the room's default.
                self.assertEqual(
                    engine.room_policy("room-kitchen"),
                    {"participates": True, "eligible_entity_ids": [], "default": True},
                )
                self.assertTrue(engine.room_participates("room-kitchen"))

    def test_a_room_takes_part_by_default_and_a_stored_policy_wins(self) -> None:
        engine = make_engine()
        self.assertEqual(
            engine.room_policy("room-kitchen"),
            {"participates": True, "eligible_entity_ids": [], "default": True},
        )
        self.assertTrue(engine.room_participates("room-kitchen"))
        engine.set_room_policy("room-kitchen", False, ["light.kitchen"])
        self.assertEqual(
            engine.room_policy("room-kitchen"),
            {
                "participates": False,
                "eligible_entity_ids": ["light.kitchen"],
                "default": False,
            },
        )
        self.assertFalse(engine.room_participates("room-kitchen"))
        engine.forget_room("room-kitchen")
        self.assertTrue(engine.room_policy("room-kitchen")["default"])

    def test_a_room_without_a_policy_switches_its_lights_fans_and_relays(
        self,
    ) -> None:
        # (entity id, attributes, registry gang type, lighting_kind: what one
        # tap may turn on, room_off_kind: what room off switches)
        for entity_id, attributes, gang, kind, off_kind in (
            ("light.kitchen", {}, None, "light", "light"),
            ("fan.kitchen", {}, None, "fan", "fan"),
            ("light.lamp", {"casasmart_room_activity_exclude": True}, None, None, None),
            # Wall relays the installer typed as a light or fan.
            ("switch.relay", {}, "light", "light", "light"),
            ("switch.relay", {"device_class": "switch"}, "light", "light", "light"),
            ("switch.fan_relay", {}, "fan", "fan", "fan"),
            # Untyped wall relays: room off switches them, one tap
            # doesn't turn them back on.
            ("switch.relay", {"device_class": "switch"}, "switch", None, "switch"),
            ("switch.relay", {}, "switch", None, "switch"),
            ("switch.untyped", {}, None, None, "switch"),
            ("switch.untyped", {"device_class": "switch"}, None, None, "switch"),
            # Home Assistant says outlet: never, whatever the gang type.
            ("switch.relay", {"device_class": "outlet"}, "light", None, None),
            ("switch.socket", {"device_class": "outlet"}, "switch", None, None),
            ("switch.socket", {"device_class": "outlet"}, None, None, None),
            (
                "switch.relay",
                {"casasmart_room_activity_exclude": True},
                "light",
                None,
                None,
            ),
            (
                "switch.relay",
                {"casasmart_room_activity_exclude": True},
                None,
                None,
                None,
            ),
            ("switch.kettle", {"device_class": "outlet"}, "outlet", None, None),
            ("switch.plug", {}, "outlet", None, None),
            ("switch.plug", {}, "plug", None, None),
            ("switch.heater", {"device_class": "switch"}, "heater", None, None),
            ("switch.heater", {}, "heater", None, None),
            ("climate.ac", {"hvac_modes": ["off", "cool"]}, "light", None, None),
            ("cover.curtain", {}, None, None, None),
            ("media_player.tv", {}, None, None, None),
            ("water_heater.tank", {}, None, None, None),
        ):
            with self.subTest(entity_id=entity_id, gang=gang, attributes=attributes):
                state = {"entity_id": entity_id, "attributes": attributes}
                self.assertEqual(_NOW.lighting_kind(state, gang), kind)
                self.assertEqual(_NOW.room_off_kind(state, gang), off_kind)
                self.assertIs(
                    _NOW.is_default_room_device(state, gang), off_kind is not None
                )
                self.assertIs(
                    _NOW.is_room_activity_candidate(state, gang), off_kind is not None
                )

    def test_a_room_is_on_by_what_room_off_switches_and_a_running_ac(self) -> None:
        # room_on_kind is what makes a room on: what
        # room_command_approves (what a policy decides), and an AC in any
        # room, which no policy lists. room_light_kind (one tap's lights and
        # fans) is always one of the devices a policy decides, never an
        # untyped relay.
        gang_types = {
            "light.main": None,
            "fan.hood": None,
            "switch.wall": "light",
            "switch.fridge": "outlet",
            "switch.relay": "switch",
            "switch.boiler": "heater",
            "switch.socket": "light",
            "switch.untyped": None,
            "climate.ac": None,
            "climate.heat_only": None,
        }
        attributes = {
            "switch.relay": {"device_class": "switch"},
            "switch.boiler": {"device_class": "switch"},
            "switch.socket": {"device_class": "outlet"},
            "climate.ac": {"hvac_modes": ["off", "cool"]},
            "climate.heat_only": {"hvac_modes": ["off", "heat"]},
        }

        def state(entity_id):
            return {"entity_id": entity_id, "attributes": attributes.get(entity_id, {})}

        default = _NOW.default_room_policy()
        self.assertEqual(default, _NOW.NowDataEngine(*[Table()] * 5).room_policy("r"))
        stored = {
            "participates": True,
            "eligible_entity_ids": [
                "light.main",
                "switch.relay",
                "switch.boiler",
                "switch.fridge",
            ],
            "default": False,
        }
        # entity id -> ((room_on_kind, room_light_kind, a policy approves it)
        # with no stored policy, the same under the stored list)
        expected = {
            "light.main": (("light", "light", True), ("light", "light", True)),
            "fan.hood": (("fan", "fan", True), (None, None, False)),
            "switch.wall": (("light", "light", True), (None, None, False)),
            # An untyped relay: switched and counted, never turned back on.
            "switch.relay": (("switch", None, True), ("switch", None, True)),
            "switch.untyped": (("switch", None, True), (None, None, False)),
            # A plug or a heater: never, even on an admin's list (stored
            # earlier, when a heater relay could be approved).
            "switch.fridge": ((None, None, False), (None, None, False)),
            "switch.boiler": ((None, None, False), (None, None, False)),
            "switch.socket": ((None, None, False), (None, None, False)),
            # An AC: on while it runs, in any room; no policy decides it
            # (room off switches it by is_room_ac).
            "climate.ac": (("ac", None, False), ("ac", None, False)),
            "climate.heat_only": ((None, None, False), (None, None, False)),
            # Not imported into the registry.
            "light.unimported": ((None, None, False), (None, None, False)),
        }
        for entity_id, by_policy in expected.items():
            item = state(entity_id)
            for policy, (on_kind, light_kind, switched) in zip(
                (default, stored), by_policy, strict=True
            ):
                with self.subTest(entity_id=entity_id, default=policy["default"]):
                    self.assertEqual(
                        _NOW.room_on_kind(item, policy, gang_types), on_kind
                    )
                    self.assertEqual(
                        _NOW.room_light_kind(item, policy, gang_types), light_kind
                    )
                    self.assertIs(
                        _NOW.room_command_approves(item, policy, gang_types), switched
                    )
                    # Everything a policy approves counts; one tap's lights
                    # and fans are only ever some of it.
                    self.assertIs(
                        switched,
                        on_kind is not None and on_kind != _NOW.KIND_AC,
                    )
                    if light_kind is not None:
                        self.assertTrue(switched)

    def test_a_wall_relay_typed_as_a_light_is_a_room_activity_candidate(
        self,
    ) -> None:
        for attributes, gang, expected in (
            ({}, "light", True),
            ({}, "fan", True),
            ({"device_class": "switch"}, None, True),
            # An untyped wall relay.
            ({}, None, True),
            ({}, "switch", True),
            ({}, "outlet", False),
            ({}, "heater", False),
            # A heater or outlet relay Home Assistant calls a switch: no
            # longer (never switched by a room command).
            ({"device_class": "switch"}, "heater", False),
            ({"device_class": "switch"}, "outlet", False),
            ({"device_class": "outlet"}, "light", False),
            ({"device_class": "outlet"}, None, False),
        ):
            with self.subTest(attributes=attributes, gang=gang):
                self.assertIs(
                    _NOW.is_room_activity_candidate(
                        {"entity_id": "switch.relay", "attributes": attributes}, gang
                    ),
                    expected,
                )

    def test_room_policy_and_config_are_stored_as_given(self) -> None:
        engine = make_engine()
        self.assertEqual(
            engine.set_room_policy(
                "room-kitchen", True, ["light.kitchen", "fan.kitchen"]
            ),
            {
                "room_id": "room-kitchen",
                "participates": True,
                "eligible_entity_ids": ["light.kitchen", "fan.kitchen"],
            },
        )
        self.assertEqual(
            engine.room_policy("room-kitchen")["eligible_entity_ids"],
            ["light.kitchen", "fan.kitchen"],
        )
        config = engine.configure(
            {
                "outdoor_weather_entity_id": "weather.openweathermap",
                "air_quality_entity_id": "sensor.air_quality",
                "contact_entity_ids": ["binary_sensor.front_door", "lock.gate"],
                "suggested_scene_id": "scene-leave",
                "pinned_scene_ids": ["scene-arrive", "scene-night"],
            }
        )
        self.assertEqual(config["outdoor_weather_entity_id"], "weather.openweathermap")
        self.assertEqual(
            config["contact_entity_ids"],
            ["binary_sensor.front_door", "lock.gate"],
        )
        for unrelated in (
            "sensor.not_a_contact",
            "switch.gate",
            "cover.gate",
            "alarm_control_panel.home",
        ):
            with (
                self.subTest(unrelated=unrelated),
                self.assertRaises(_NOW.NowDataError),
            ):
                engine.configure({"contact_entity_ids": [unrelated]})

    def test_restore_and_idempotency_are_durable_and_bounded_to_actor(self) -> None:
        engine = make_engine()
        key = engine.validate_idempotency_key("room-off-20260824")
        engine.save_restore_set("room-kitchen", ["light.kitchen", "fan.kitchen"])
        self.assertEqual(
            engine.restore_set("room-kitchen"), ["light.kitchen", "fan.kitchen"]
        )
        result = {
            "ok": True,
            "outcomes": [{"entity_id": "light.kitchen", "outcome": "changed"}],
        }
        engine.save_idempotent_result(
            "member-a", "room-kitchen", "turn_off", key, result
        )
        self.assertEqual(
            engine.idempotent_result("member-a", "room-kitchen", "turn_off", key),
            result,
        )
        self.assertIsNone(
            engine.idempotent_result("member-a", "room-kitchen", "turn_on", key)
        )
        self.assertIsNone(
            engine.idempotent_result("member-b", "room-kitchen", "turn_off", key)
        )
        self.assertEqual(
            engine.consume_restore_set("room-kitchen"), ["light.kitchen", "fan.kitchen"]
        )
        self.assertEqual(engine.restore_set("room-kitchen"), [])

    def test_a_later_room_off_never_makes_an_old_capture_new(self) -> None:
        # Every room off used to restamp the capture, so
        # a tap that switched nothing kept yesterday's capture "fresh".
        from datetime import timedelta

        engine = make_engine()
        t = datetime(2026, 10, 23, 17, tzinfo=UTC)
        engine.extend_restore_set("room-kitchen", ["light.main", "light.spot"], t)
        stamp = t.isoformat()
        # A second tap a minute later adds to it and keeps the time.
        engine.extend_restore_set(
            "room-kitchen", ["fan.hood"], t + timedelta(seconds=60)
        )
        self.assertEqual(
            engine.restore_capture("room-kitchen"),
            (["light.main", "light.spot", "fan.hood"], stamp),
        )
        # A tap that switches nothing, a day later: the capture is untouched.
        engine.extend_restore_set("room-kitchen", [], t + timedelta(hours=25))
        self.assertEqual(engine.restore_capture("room-kitchen")[1], stamp)
        # A later off is what was on last time: a new capture.
        later = t + timedelta(hours=25)
        engine.extend_restore_set("room-kitchen", ["light.lamp"], later)
        self.assertEqual(
            engine.restore_capture("room-kitchen"), (["light.lamp"], later.isoformat())
        )

    def test_extending_a_restore_set_keeps_what_is_outstanding(self) -> None:
        engine = make_engine()
        self.assertEqual(engine.extend_restore_set("room-kitchen", []), [])
        self.assertEqual(engine.restore_set("room-kitchen"), [])

        engine.extend_restore_set("room-kitchen", ["light.kitchen", "fan.kitchen"])
        self.assertEqual(
            engine.extend_restore_set("room-kitchen", []),
            ["light.kitchen", "fan.kitchen"],
        )
        self.assertEqual(
            engine.extend_restore_set("room-kitchen", ["switch.kettle", "fan.kitchen"]),
            ["light.kitchen", "fan.kitchen", "switch.kettle"],
        )
        self.assertEqual(
            engine.consume_restore_set("room-kitchen"),
            ["light.kitchen", "fan.kitchen", "switch.kettle"],
        )
        self.assertEqual(engine.restore_set("room-kitchen"), [])

    def test_keeping_part_of_a_restore_set_keeps_its_order_and_capture_time(
        self,
    ) -> None:
        engine = make_engine()
        self.assertEqual(engine.keep_in_restore_set("room-kitchen", ["fan.a"]), [])
        engine.save_restore_set("room-kitchen", ["light.a", "fan.a", "switch.b"])
        captured_at = engine._restores["room-kitchen"]["captured_at"]

        self.assertEqual(
            engine.keep_in_restore_set("room-kitchen", ["switch.b", "light.a", "x.y"]),
            ["light.a", "switch.b"],
        )
        self.assertEqual(
            engine._restores["room-kitchen"],
            {"entity_ids": ["light.a", "switch.b"], "captured_at": captured_at},
        )
        self.assertEqual(engine.keep_in_restore_set("room-kitchen", []), [])
        self.assertNotIn("room-kitchen", engine._restores)

    def test_the_capture_says_when_it_was_made(self) -> None:
        engine = make_engine()
        self.assertEqual(engine.restore_capture("room-kitchen"), ([], None))
        engine.extend_restore_set("room-kitchen", ["light.a"])
        ids, captured_at = engine.restore_capture("room-kitchen")
        self.assertEqual(ids, ["light.a"])
        self.assertIsNotNone(datetime.fromisoformat(captured_at).tzinfo)
        for record in ({"entity_ids": "light.a"}, ["light.a"], {"captured_at": 5}):
            with self.subTest(record=record):
                engine._restores["room-x"] = record
                self.assertEqual(engine.restore_capture("room-x"), ([], None))

    def test_start_profiles_are_per_room_and_go_with_the_room(self) -> None:
        profiles = Table()
        engine = _NOW.NowDataEngine(
            Table(), Table(), Table(), Table(), Table(), start_profiles_table=profiles
        )
        self.assertEqual(engine.start_profile("room-kitchen"), [])
        self.assertEqual(
            engine.set_start_profile("room-kitchen", ["light.a", "fan.b"]),
            {"room_id": "room-kitchen", "entity_ids": ["light.a", "fan.b"]},
        )
        engine.set_start_profile("room-majlis", ["light.m"])
        self.assertEqual(profiles["room-kitchen"], {"entity_ids": ["light.a", "fan.b"]})
        for bad in ([], ["light.a", "light.a"], ["nodot"], "light.a", [1], None):
            with self.subTest(bad=bad):
                with self.assertRaises(_NOW.NowDataError):
                    engine.set_start_profile("room-kitchen", bad)
        with self.assertRaises(_NOW.NowDataError):
            engine.set_start_profile("room-kitchen", [f"light.l{i}" for i in range(65)])
        self.assertEqual(engine.start_profile("room-kitchen"), ["light.a", "fan.b"])
        self.assertEqual(
            engine.clear_start_profile("room-kitchen"),
            {"room_id": "room-kitchen", "entity_ids": []},
        )
        self.assertEqual(engine.start_profile("room-kitchen"), [])
        engine.forget_room("room-majlis")
        self.assertEqual(engine.start_profile("room-majlis"), [])
        self.assertEqual(dict(profiles), {})
        # A member leaving doesn't touch them: they are the room's.
        engine.set_start_profile("room-kitchen", ["light.a"])
        engine.forget_member("member-a")
        self.assertEqual(engine.start_profile("room-kitchen"), ["light.a"])


class RoomActivityContractTest(unittest.TestCase):
    def test_opening_aggregate_distinguishes_lock_truth_from_transient_states(
        self,
    ) -> None:
        self.assertEqual(
            _NOW.summarize_openings(
                [
                    ("binary_sensor.front_door", "on"),
                    ("binary_sensor.window", "off"),
                    ("lock.gate", "unlocked"),
                    ("lock.side_door", "locked"),
                ]
            ),
            {"status": "open", "open_count": 2, "unknown_count": 0},
        )
        self.assertEqual(
            _NOW.summarize_openings(
                [
                    ("binary_sensor.front_door", "off"),
                    ("lock.gate", "locked"),
                ]
            ),
            {"status": "all_closed", "open_count": 0, "unknown_count": 0},
        )
        for state in ("locking", "unlocking", "jammed", "unavailable", "unknown", None):
            with self.subTest(state=state):
                self.assertEqual(
                    _NOW.summarize_openings([("lock.gate", state)]),
                    {"status": "unknown", "open_count": 0, "unknown_count": 1},
                )

    def test_allowlist_requires_hub_policy_and_excludes_plugs_and_heaters(self) -> None:
        allowed = ["light.kitchen", "fan.kitchen", "switch.wall"]
        self.assertTrue(
            _NOW.is_room_activity_eligible(
                {"entity_id": "light.kitchen", "state": "on"}, allowed
            )
        )
        self.assertTrue(
            _NOW.is_room_activity_eligible(
                {"entity_id": "fan.kitchen", "state": "on"}, allowed
            )
        )
        self.assertTrue(
            _NOW.is_room_activity_eligible(
                {"entity_id": "switch.wall", "attributes": {"device_class": "switch"}},
                allowed,
            )
        )
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {"entity_id": "light.unlisted", "state": "on"}, allowed
            )
        )
        # A switch without a device class is an untyped wall relay:
        # on the list, room off switches it; typed as a heater, never.
        self.assertTrue(
            _NOW.is_room_activity_eligible(
                {"entity_id": "switch.generic"}, ["switch.generic"]
            )
        )
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {"entity_id": "switch.generic"}, ["switch.generic"], "heater"
            )
        )
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {
                    "entity_id": "switch.coffee_machine",
                    "attributes": {"device_class": "outlet"},
                },
                ["switch.coffee_machine"],
            )
        )
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {"entity_id": "lock.front_door"}, ["lock.front_door"]
            )
        )
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {"entity_id": "cover.curtain"}, ["cover.curtain"]
            )
        )
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {"entity_id": "camera.gate"}, ["camera.gate"]
            )
        )

    def test_fixed_layout_contract(self) -> None:
        rooms = [{"room_id": f"room-{index}"} for index in range(6)]
        self.assertEqual(
            _NOW.room_activity_layout([]),
            {"featured_room_id": None, "cards": [], "view_all_count": 0},
        )
        self.assertEqual(_NOW.room_activity_layout(rooms[:4])["featured_room_id"], None)
        self.assertEqual(
            _NOW.room_activity_layout(rooms[:5]),
            {
                "featured_room_id": "room-0",
                "cards": ["room-1", "room-2", "room-3", "room-4"],
                "view_all_count": 0,
            },
        )
        self.assertEqual(_NOW.room_activity_layout(rooms)["view_all_count"], 1)


if __name__ == "__main__":
    unittest.main()
