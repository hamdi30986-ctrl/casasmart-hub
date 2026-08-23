"""Focused contract tests for the Hub-owned Now data foundation."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest


_MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "casasmart" / "now_data.py"
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
        early = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
        late = datetime(2026, 8, 24, 9, tzinfo=timezone.utc)
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

    def test_room_policy_and_config_are_explicit_not_auto_discovered(self) -> None:
        engine = make_engine()
        self.assertFalse(engine.room_participates("room-kitchen"))
        self.assertEqual(
            engine.set_room_policy("room-kitchen", True, ["light.kitchen", "fan.kitchen"]),
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
                "contact_entity_ids": ["binary_sensor.front_door"],
                "suggested_scene_id": "scene-leave",
                "pinned_scene_ids": ["scene-arrive", "scene-night"],
            }
        )
        self.assertEqual(config["outdoor_weather_entity_id"], "weather.openweathermap")
        self.assertEqual(config["contact_entity_ids"], ["binary_sensor.front_door"])
        with self.assertRaises(_NOW.NowDataError):
            engine.configure({"contact_entity_ids": ["sensor.not_a_contact"]})

    def test_restore_and_idempotency_are_durable_and_bounded_to_actor(self) -> None:
        engine = make_engine()
        key = engine.validate_idempotency_key("room-off-20260824")
        engine.save_restore_set("room-kitchen", ["light.kitchen", "fan.kitchen"])
        self.assertEqual(engine.restore_set("room-kitchen"), ["light.kitchen", "fan.kitchen"])
        result = {"ok": True, "outcomes": [{"entity_id": "light.kitchen", "outcome": "changed"}]}
        engine.save_idempotent_result("member-a", "room-kitchen", "turn_off", key, result)
        self.assertEqual(engine.idempotent_result("member-a", "room-kitchen", "turn_off", key), result)
        self.assertIsNone(engine.idempotent_result("member-a", "room-kitchen", "turn_on", key))
        self.assertIsNone(engine.idempotent_result("member-b", "room-kitchen", "turn_off", key))
        self.assertEqual(engine.consume_restore_set("room-kitchen"), ["light.kitchen", "fan.kitchen"])
        self.assertEqual(engine.restore_set("room-kitchen"), [])


class RoomActivityContractTest(unittest.TestCase):
    def test_allowlist_requires_hub_policy_and_excludes_generic_switches(self) -> None:
        allowed = ["light.kitchen", "fan.kitchen", "switch.wall"]
        self.assertTrue(_NOW.is_room_activity_eligible({"entity_id": "light.kitchen", "state": "on"}, allowed))
        self.assertTrue(_NOW.is_room_activity_eligible({"entity_id": "fan.kitchen", "state": "on"}, allowed))
        self.assertTrue(_NOW.is_room_activity_eligible({"entity_id": "switch.wall", "attributes": {"device_class": "switch"}}, allowed))
        self.assertFalse(_NOW.is_room_activity_eligible({"entity_id": "light.unlisted", "state": "on"}, allowed))
        self.assertFalse(_NOW.is_room_activity_eligible({"entity_id": "switch.generic"}, ["switch.generic"]))
        self.assertFalse(_NOW.is_room_activity_eligible({"entity_id": "switch.coffee_machine", "attributes": {"device_class": "outlet"}}, ["switch.coffee_machine"]))
        self.assertFalse(_NOW.is_room_activity_eligible({"entity_id": "lock.front_door"}, ["lock.front_door"]))
        self.assertFalse(_NOW.is_room_activity_eligible({"entity_id": "cover.curtain"}, ["cover.curtain"]))
        self.assertFalse(_NOW.is_room_activity_eligible({"entity_id": "camera.gate"}, ["camera.gate"]))

    def test_fixed_layout_contract(self) -> None:
        rooms = [{"room_id": f"room-{index}"} for index in range(6)]
        self.assertEqual(_NOW.room_activity_layout([]), {"featured_room_id": None, "cards": [], "view_all_count": 0})
        self.assertEqual(_NOW.room_activity_layout(rooms[:4])["featured_room_id"], None)
        self.assertEqual(_NOW.room_activity_layout(rooms[:5]), {"featured_room_id": "room-0", "cards": ["room-1", "room-2", "room-3", "room-4"], "view_all_count": 0})
        self.assertEqual(_NOW.room_activity_layout(rooms)["view_all_count"], 1)


if __name__ == "__main__":
    unittest.main()
