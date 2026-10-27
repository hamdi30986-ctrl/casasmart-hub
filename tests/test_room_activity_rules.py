"""room_intel.activity: the event, attribution and retention rules (no HA)."""

import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"
sys.path.insert(0, str(_PKG))

from storage import HubStorage  # noqa: E402

# Loaded by path: the module imports nothing from the package or from HA.
_spec = importlib.util.spec_from_file_location(
    "room_activity_rules", _PKG / "room_intel" / "activity.py"
)
activity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(activity)


class TransitionTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("light.a", "off", "on", "on"),
            ("light.a", "on", "off", "off"),
            ("fan.a", "off", "on", "on"),
            ("switch.a", "on", "off", "off"),
            ("light.a", "on", "on", None),
            ("light.a", "unavailable", "on", None),
            ("light.a", "on", "unavailable", None),
            ("light.a", "unknown", "off", None),
            ("climate.ac", "off", "cool", "on"),
            ("climate.ac", "heat_cool", "off", "off"),
            ("climate.ac", "cool", "dry", "set"),
            ("climate.ac", "cool", "cool", None),
            ("climate.ac", "unavailable", "cool", None),
            ("climate.ac", "off", "bogus", None),
            ("cover.c", "closed", "opening", "open"),
            ("cover.c", "closing", "open", "open"),
            ("cover.c", "opening", "open", None),
            ("cover.c", "open", "closing", "close"),
            ("cover.c", "opening", "closing", "close"),
            ("cover.c", "closing", "closed", None),
            ("cover.c", "unknown", "open", None),
            ("media_player.tv", "off", "playing", "on"),
            ("media_player.tv", "standby", "on", "on"),
            ("media_player.tv", "playing", "paused", None),
            ("media_player.tv", "idle", "off", "off"),
            ("media_player.tv", "unavailable", "playing", None),
            ("sensor.power", "1", "2", None),
            ("lock.door", "locked", "unlocked", None),
        ]
        for entity_id, old, new, expected in cases:
            with self.subTest(entity_id=entity_id, old=old, new=new):
                self.assertEqual(activity.transition(entity_id, old, new), expected)


class AttributeTests(unittest.TestCase):
    def attr(self, **overrides):
        values = {
            "hub_origin": None,
            "user_id": None,
            "parent_id": None,
            "automation_context": False,
            "service_call_context": False,
        }
        values.update(overrides)
        return activity.attribute(**values)

    def test_precedence(self):
        self.assertEqual(
            self.attr(hub_origin=("app", "mem-a"), user_id="u", parent_id="p"),
            ("app", "mem-a"),
        )
        self.assertEqual(self.attr(hub_origin=("scene", "mem-a")), ("scene", "mem-a"))
        # Energy Saving's calls never carry a member.
        self.assertEqual(
            self.attr(hub_origin=("automation", "mem-a")), ("automation", None)
        )
        self.assertEqual(
            self.attr(user_id="u", automation_context=True), ("ha_user", None)
        )
        self.assertEqual(self.attr(parent_id="p"), ("automation", None))
        self.assertEqual(
            self.attr(automation_context=True, service_call_context=True),
            ("automation", None),
        )
        self.assertEqual(self.attr(service_call_context=True), ("unknown", None))
        self.assertEqual(self.attr(), ("wall_switch", None))

    def test_sources_and_hub_sources(self):
        self.assertEqual(
            activity.SOURCES,
            {"app", "scene", "automation", "ha_user", "wall_switch", "unknown"},
        )
        self.assertEqual(activity.HUB_SOURCES, {"app", "scene", "automation"})


class ChatterGuardTests(unittest.TestCase):
    def test_burst_then_hourly_rate(self):
        guard = activity.ChatterGuard(burst=3, per_hour=6)  # a token per 10 min
        self.assertEqual(
            [guard.allow("light.a", 0.0) for _ in range(4)], [True] * 3 + [False]
        )
        self.assertTrue(guard.allow("light.b", 0.0))  # per device
        self.assertFalse(guard.allow("light.a", 599.0))
        self.assertTrue(guard.allow("light.a", 600.0))
        self.assertFalse(guard.allow("light.a", 601.0))
        # A quiet hour refills the burst, no further.
        self.assertEqual(
            [guard.allow("light.a", 600.0 + 7200) for _ in range(4)],
            [True] * 3 + [False],
        )

    def test_forget_except(self):
        guard = activity.ChatterGuard(burst=1, per_hour=1)
        guard.allow("light.a", 0.0)
        guard.allow("light.b", 0.0)
        guard.forget_except(["light.b"])
        self.assertTrue(guard.allow("light.a", 0.0))  # fresh bucket
        self.assertFalse(guard.allow("light.b", 0.0))


class RecentIdsTests(unittest.TestCase):
    def test_ttl_and_size(self):
        ids = activity.RecentIds(ttl=10.0, size=2)
        ids.add("a", 1, 0.0)
        ids.add("b", 2, 5.0)
        self.assertEqual(ids.get("a", 9.9), 1)
        self.assertIsNone(ids.get("a", 10.0))
        self.assertIsNone(ids.get(None, 0.0))
        ids.add("c", 3, 6.0)  # over size: the oldest goes
        self.assertIsNone(ids.get("a", 6.0))
        self.assertEqual((ids.get("b", 6.0), ids.get("c", 6.0)), (2, 3))
        ids.discard_values(lambda value: value == 3)
        self.assertIsNone(ids.get("c", 6.0))


class PruneTests(unittest.TestCase):
    def test_retention_numbers(self):
        self.assertEqual(activity.HUMAN_RETENTION_DAYS, 400)
        self.assertEqual(activity.AUTOMATION_RETENTION_DAYS, 31)
        self.assertEqual(activity.MAX_ROWS, 500_000)
        # Last year's Ramadan is still there when this year's ends: a lunar
        # year (354.37 days) plus the 30 days of Ramadan.
        self.assertGreater(activity.HUMAN_RETENTION_DAYS, 354.37 + 30)

    def test_prune_against_the_real_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = HubStorage(Path(tmp) / "hub.db")
            storage.open()
            try:
                table = storage.room_activity()
                now, day = 1_800_000_000, 86400
                table.append_many(
                    {
                        "t": now - age * day,
                        "utc_offset_min": 180,
                        "room_id": "r",
                        "entity_id": "light.a",
                        "action": "on",
                        "source": source,
                    }
                    for age, source in (
                        (1, "automation"),
                        (31, "automation"),
                        (32, "automation"),
                        (32, "wall_switch"),
                        (400, "app"),
                        (401, "app"),
                    )
                )
                # A row exactly at its limit stays; older ones go.
                self.assertEqual(
                    activity.prune(table, now), {"automation": 1, "age": 1, "cap": 0}
                )
                self.assertEqual(
                    sorted(
                        ((now - e["t"]) // day, e["source"]) for e in table.events()
                    ),
                    [
                        (1, "automation"),
                        (31, "automation"),
                        (32, "wall_switch"),
                        (400, "app"),
                    ],
                )
            finally:
                storage.close()


class HistogramTests(unittest.TestCase):
    def test_folds_days_into_hours(self):
        rows = [
            {"room_id": "a", "local_hour": 19, "events": 2},
            {"room_id": "a", "local_hour": 19, "events": 1},
            {"room_id": "a", "local_hour": 0, "events": 4},
            {"room_id": "b", "local_hour": 23, "events": 1},
        ]
        histogram = activity.hour_histogram(rows)
        self.assertEqual(sorted(histogram), ["a", "b"])
        self.assertEqual(histogram["a"][19], 3)
        self.assertEqual(histogram["a"][0], 4)
        self.assertEqual(sum(histogram["a"]), 7)
        self.assertEqual(histogram["b"], [0] * 23 + [1])
        self.assertEqual(activity.hour_histogram([]), {})


class LevelRulesTests(unittest.TestCase):
    def test_a_lights_level_is_its_brightness_and_colour_temperature(self):
        self.assertEqual(
            activity.level_of(
                "light.majlis",
                "on",
                {
                    "brightness": 204,
                    "color_temp_kelvin": 2700,
                    "color_mode": "color_temp",
                },
            ),
            {"brightness": 204, "color_temp_kelvin": 2700},
        )
        # In a colour mode Home Assistant reports no colour temperature.
        self.assertEqual(
            activity.level_of(
                "light.majlis",
                "on",
                {"brightness": 51, "color_temp_kelvin": None, "hs_color": [30, 80]},
            ),
            {"brightness": 51},
        )
        # A float brightness is rounded; nonsense is left out.
        self.assertEqual(
            activity.level_of("light.a", "on", {"brightness": 127.6}),
            {"brightness": 128},
        )
        for attributes in (
            {},
            {"brightness": None},
            {"brightness": 0},
            {"brightness": 256},
            {"brightness": True},
            {"brightness": "200"},
            {"brightness": math.nan},
            {"color_temp_kelvin": -1},
        ):
            with self.subTest(attributes=attributes):
                self.assertIsNone(activity.level_of("light.a", "on", attributes))
        self.assertIsNone(activity.level_of("light.a", "off", {"brightness": 200}))

    def test_a_fans_level_is_its_speed_and_an_acs_its_mode_target_and_fan(self):
        self.assertEqual(
            activity.level_of("fan.majlis", "on", {"percentage": 66}),
            {"percentage": 66},
        )
        self.assertIsNone(activity.level_of("fan.majlis", "on", {"percentage": 0}))
        self.assertIsNone(activity.level_of("fan.majlis", "on", {"percentage": 101}))
        self.assertEqual(
            activity.level_of(
                "climate.majlis",
                "cool",
                {"temperature": 23, "fan_mode": "low", "current_temperature": 29},
            ),
            {"hvac_mode": "cool", "temperature": 23.0, "fan_mode": "low"},
        )
        # A mode without a target (fan only) still has its mode.
        self.assertEqual(
            activity.level_of("climate.majlis", "fan_only", {"temperature": None}),
            {"hvac_mode": "fan_only"},
        )
        self.assertIsNone(
            activity.level_of("climate.majlis", "off", {"temperature": 23})
        )
        self.assertEqual(
            activity.level_of("climate.a", "heat", {"temperature": math.inf}),
            {"hvac_mode": "heat"},
        )
        # A wall relay, a cover, a TV have no level.
        for entity_id, state in (
            ("switch.majlis", "on"),
            ("cover.majlis", "open"),
            ("media_player.tv", "on"),
        ):
            self.assertIsNone(activity.level_of(entity_id, state, {"brightness": 9}))

    def test_a_level_change_is_a_new_level_on_a_device_that_stays_on(self):
        change = activity.level_change
        self.assertEqual(
            change("light.a", "on", {"brightness": 200}, "on", {"brightness": 90}),
            {"brightness": 90},
        )
        self.assertEqual(
            change(
                "climate.a",
                "cool",
                {"temperature": 24, "current_temperature": 28},
                "cool",
                {"temperature": 22, "current_temperature": 28},
            ),
            {"hvac_mode": "cool", "temperature": 22.0},
        )
        # Not one: the room's temperature moving, the same level reported
        # again, a switch on or off, an AC's mode change (a "set").
        for args in (
            ("climate.a", "cool", {"temperature": 24, "current_temperature": 28},
             "cool", {"temperature": 24, "current_temperature": 27}),
            ("light.a", "on", {"brightness": 90}, "on", {"brightness": 90}),
            ("light.a", "off", {}, "on", {"brightness": 90}),
            ("light.a", "on", {"brightness": 90}, "off", {}),
            ("climate.a", "cool", {"temperature": 24}, "heat", {"temperature": 24}),
            ("light.a", "on", {"brightness": 90}, "on", {"brightness": None}),
            ("switch.a", "on", {}, "on", {"x": 1}),
        ):  # fmt: skip
            with self.subTest(args=args):
                self.assertIsNone(change(*args))

    def test_level_rows_come_from_the_sources_the_habits_learn_from(self):
        from room_intel.habits import HUMAN_SOURCES

        self.assertEqual(activity.LEVEL_SOURCES, frozenset(HUMAN_SOURCES))
        self.assertEqual(
            activity.LEVEL_ACTIONS, {activity.ACTION_ON, activity.ACTION_SET, "level"}
        )


if __name__ == "__main__":
    unittest.main()
