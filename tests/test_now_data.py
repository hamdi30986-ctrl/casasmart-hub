"""Focused contract tests for the Hub-owned Now data foundation."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime
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

    def test_a_room_without_a_policy_switches_its_lights_and_fans(self) -> None:
        # (entity id, attributes, registry gang type, lighting_kind)
        for entity_id, attributes, gang, kind in (
            ("light.kitchen", {}, None, "light"),
            ("fan.kitchen", {}, None, "fan"),
            ("light.lamp", {"casasmart_room_activity_exclude": True}, None, None),
            # Wall relays the installer typed as a light or fan.
            ("switch.relay", {}, "light", "light"),
            ("switch.relay", {"device_class": "switch"}, "light", "light"),
            ("switch.fan_relay", {}, "fan", "fan"),
            # Home Assistant says outlet: never, whatever the gang type.
            ("switch.relay", {"device_class": "outlet"}, "light", None),
            (
                "switch.relay",
                {"casasmart_room_activity_exclude": True},
                "light",
                None,
            ),
            ("switch.kettle", {"device_class": "outlet"}, "outlet", None),
            ("switch.plug", {}, "outlet", None),
            ("switch.heater", {"device_class": "switch"}, "heater", None),
            ("switch.relay", {"device_class": "switch"}, "switch", None),
            ("switch.untyped", {}, None, None),
            ("climate.ac", {"hvac_modes": ["off", "cool"]}, "light", None),
            ("cover.curtain", {}, None, None),
            ("media_player.tv", {}, None, None),
            ("water_heater.tank", {}, None, None),
        ):
            with self.subTest(entity_id=entity_id, gang=gang, attributes=attributes):
                state = {"entity_id": entity_id, "attributes": attributes}
                self.assertEqual(_NOW.lighting_kind(state, gang), kind)
                self.assertIs(
                    _NOW.is_default_room_device(state, gang), kind is not None
                )

    def test_a_room_is_on_only_by_what_room_off_switches(self) -> None:
        # room_light_kind is what makes a room on;
        # it is always something room_command_approves (room off switches).
        gang_types = {
            "light.main": None,
            "fan.hood": None,
            "switch.wall": "light",
            "switch.fridge": "outlet",
            "switch.relay": "switch",
            "switch.boiler": "heater",
            "switch.socket": "light",
            "climate.ac": None,
        }
        attributes = {
            "switch.relay": {"device_class": "switch"},
            "switch.boiler": {"device_class": "switch"},
            "switch.socket": {"device_class": "outlet"},
            "climate.ac": {"hvac_modes": ["off", "cool"]},
        }

        def state(entity_id):
            return {"entity_id": entity_id, "attributes": attributes.get(entity_id, {})}

        default = _NOW.default_room_policy()
        self.assertEqual(default, _NOW.NowDataEngine(*[Table()] * 5).room_policy("r"))
        stored = {
            "participates": True,
            "eligible_entity_ids": ["light.main", "switch.relay", "switch.boiler"],
            "default": False,
        }
        # entity id -> (kind with no stored policy, kind under the stored list,
        # room off switches it under the stored list)
        expected = {
            "light.main": ("light", "light", True),
            "fan.hood": ("fan", None, False),
            "switch.wall": ("light", None, False),
            "switch.fridge": (None, None, False),
            # Approved for room off by the admin, yet never makes it on.
            "switch.relay": (None, None, True),
            "switch.boiler": (None, None, True),
            "switch.socket": (None, None, False),
            "climate.ac": (None, None, False),
            # Not imported into the registry.
            "light.unimported": (None, None, False),
        }
        for entity_id, (kind, listed_kind, listed_off) in expected.items():
            with self.subTest(entity_id=entity_id):
                item = state(entity_id)
                self.assertEqual(_NOW.room_light_kind(item, default, gang_types), kind)
                self.assertIs(
                    _NOW.room_command_approves(item, default, gang_types),
                    kind is not None,
                )
                self.assertEqual(
                    _NOW.room_light_kind(item, stored, gang_types), listed_kind
                )
                self.assertIs(
                    _NOW.room_command_approves(item, stored, gang_types), listed_off
                )

    def test_a_wall_relay_typed_as_a_light_is_a_room_activity_candidate(
        self,
    ) -> None:
        for attributes, gang, expected in (
            ({}, "light", True),
            ({}, "fan", True),
            ({"device_class": "switch"}, None, True),
            ({}, None, False),
            ({}, "outlet", False),
            ({}, "heater", False),
            ({"device_class": "outlet"}, "light", False),
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

    def test_allowlist_requires_hub_policy_and_excludes_generic_switches(self) -> None:
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
        self.assertFalse(
            _NOW.is_room_activity_eligible(
                {"entity_id": "switch.generic"}, ["switch.generic"]
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
