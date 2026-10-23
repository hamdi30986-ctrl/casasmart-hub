"""room_intel.meters: which sensors are power meters and what they read (no HA)."""

import importlib.util
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"

# Loaded by path: the module imports nothing from the package or from HA.
_spec = importlib.util.spec_from_file_location(
    "room_meter_rules", _PKG / "room_intel" / "meters.py"
)
meters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(meters)


def meter(state, unit="W", entity_id="sensor.plug_power", **attributes):
    return NS(
        entity_id=entity_id,
        state=state,
        attributes={"device_class": "power", "unit_of_measurement": unit, **attributes},
    )


class PowerMeterTests(unittest.TestCase):
    def test_a_power_meter_is_a_sensor_with_device_class_power(self):
        self.assertTrue(meters.is_power_meter(meter("5")))
        # The Energy wizard's "power in the entity id" rule is not used.
        for state in [
            NS(
                entity_id="sensor.plug_power_factor",
                state="0.9",
                attributes={"device_class": "power_factor"},
            ),
            NS(
                entity_id="sensor.plug_apparent_power",
                state="40",
                attributes={
                    "device_class": "apparent_power",
                    "unit_of_measurement": "VA",
                },
            ),
            NS(entity_id="sensor.plug_power", state="5", attributes={}),
            NS(
                entity_id="number.plug_power",
                state="5",
                attributes={"device_class": "power"},
            ),
        ]:
            self.assertFalse(meters.is_power_meter(state), state.entity_id)


class WattsTests(unittest.TestCase):
    def test_units_convert_to_watts(self):
        cases = [
            ("1840", "W", 1840.0),
            ("1.84", "kW", 1840.0),
            ("2500", "mW", 2.5),
            ("0.002", "MW", 2000.0),
            ("1000", "BTU/h", 293.07107),
        ]
        for state, unit, watts in cases:
            with self.subTest(unit=unit):
                self.assertAlmostEqual(meters.watts_of(meter(state, unit)), watts)

    def test_an_unreadable_value_is_none_never_zero(self):
        for state in ["unavailable", "unknown", "", "abc", "nan", "inf", "-inf", None]:
            with self.subTest(state=state):
                self.assertIsNone(meters.watts_of(meter(state)))
        # A value too big for a float once converted.
        self.assertIsNone(meters.watts_of(meter("1e300", "TW")))

    def test_a_reading_needs_a_power_unit(self):
        for unit in [None, "", "VA", "kWh", "w"]:
            with self.subTest(unit=unit):
                self.assertIsNone(meters.watts_of(meter("100", unit)))

    def test_negative_readings_are_invalid_beyond_the_rest_tolerance(self):
        self.assertEqual(meters.watts_of(meter("-1.5")), 0.0)
        self.assertEqual(meters.watts_of(meter("-2")), 0.0)
        self.assertIsNone(meters.watts_of(meter("-2.1")))
        self.assertIsNone(meters.watts_of(meter("-3384")))
        # Never abs(): a reversed meter must not become the biggest load.
        self.assertIsNone(meters.watts_of(meter("-3.384", "kW")))

    def test_age_is_not_judged(self):
        state = meter("100")
        state.last_reported = state.last_updated = "long ago"
        self.assertEqual(meters.watts_of(state), 100.0)


class WattStepTests(unittest.TestCase):
    def test_readings_more_than_ten_percent_apart_never_share_a_step(self):
        values = [1, 1.5, 9, 10, 60, 299, 1000, 1840, 2500, 9999.5]
        for low in values:
            for factor in [1.1001, 1.25, 2, 10]:
                high = low * factor
                with self.subTest(low=low, high=high):
                    self.assertGreater(meters.watt_step(high), meters.watt_step(low))

    def test_noise_within_a_step_ties(self):
        self.assertEqual(meters.watt_step(1840), meters.watt_step(1838))
        self.assertEqual(meters.watt_step(1840), meters.watt_step(1845))

    def test_steps_are_monotonic_and_below_one_watt_is_lowest(self):
        self.assertEqual(meters.watt_step(0), -1)
        self.assertEqual(meters.watt_step(0.99), -1)
        self.assertEqual(meters.watt_step(1), 0)
        previous = -1
        watts = 1.0
        while watts < 1e6:
            step = meters.watt_step(watts)
            self.assertGreaterEqual(step, previous)
            previous = step
            watts *= 1.03
        self.assertEqual(
            meters.watt_step(1840), math.floor(math.log(1840) / math.log(1.1))
        )


if __name__ == "__main__":
    unittest.main()
