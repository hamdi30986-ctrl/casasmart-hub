"""room_intel: one ranking for the Now page and generated suggestions, and
the power-sensor lookup.

Real-HA environment only (it imports the views and runtimes as the hub does).
The ranking's answers are pinned in test_room_ranking_golden and
test_now_room_activity_golden, Energy discovery's in test_energy_power_sensor.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart import energy_api, now_api, suggestion_runtime
    from casasmart.room_intel import power, ranking
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.helpers import entity_registry as er
    from test_energy_power_sensor import FakeEntityRegistry, entry

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class SharedRankingTest(unittest.TestCase):
    def test_now_and_generated_suggestions_call_the_same_ranking(self):
        self.assertIs(now_api.async_rank_rooms, ranking.async_rank_rooms)
        self.assertIs(suggestion_runtime.async_rank_rooms, ranking.async_rank_rooms)

    def test_now_no_longer_ranks_through_the_suggestion_runtime(self):
        source = (
            Path(__file__).parents[1] / "custom_components/casasmart/now_api.py"
        ).read_text()
        self.assertNotIn("room_context", source)
        self.assertNotIn(".generated", source)


class _Hass:
    """States and hass.data: all the power lookup reads."""

    def __init__(self, entries) -> None:
        self.states = H.FakeStates()
        self.data = {er.DATA_REGISTRY: FakeEntityRegistry(entries)}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class PowerSensorTest(unittest.TestCase):
    def test_energy_discovery_uses_room_intel(self):
        self.assertIs(energy_api.power_sensor_of, power.power_sensor_of)
        self.assertFalse(hasattr(energy_api, "_power_sibling"))

    def test_device_class_power_on_the_same_device(self):
        hass = _Hass(
            [
                entry("switch.plug", "dev"),
                entry("sensor.plug_energy", "dev"),
                entry("sensor.plug_watts", "dev"),
                entry("sensor.other_watts", "other"),
            ]
        )
        hass.states.add(
            "sensor.plug_energy", state="2", attributes={"device_class": "energy"}
        )
        hass.states.add(
            "sensor.plug_watts",
            state="41",
            attributes={"device_class": "power", "unit_of_measurement": "W"},
        )
        hass.states.add(
            "sensor.other_watts", state="9", attributes={"device_class": "power"}
        )
        self.assertEqual(
            power.power_sensor_of(hass, "switch.plug"),
            {"entity_id": "sensor.plug_watts", "state": "41", "unit": "W"},
        )

    def test_registry_class_when_the_sensor_has_no_state(self):
        hass = _Hass(
            [
                entry("switch.plug", "dev"),
                entry("binary_sensor.plug_power", "dev"),
                entry("sensor.plug_load", "dev", SensorDeviceClass.POWER),
            ]
        )
        self.assertEqual(
            power.power_sensor_of(hass, "switch.plug"),
            {"entity_id": "sensor.plug_load", "state": "unavailable", "unit": None},
        )

    def test_power_in_the_entity_id_matches_first_by_entity_id(self):
        # Kept as it was: a power factor sensor wins over the real
        # power sensor when it sorts first.
        hass = _Hass(
            [
                entry("switch.plug", "dev"),
                entry("sensor.plug_power_factor", "dev"),
                entry("sensor.plug_real", "dev"),
            ]
        )
        hass.states.add("sensor.plug_power_factor", state="0.9")
        hass.states.add(
            "sensor.plug_real", state="10", attributes={"device_class": "power"}
        )
        self.assertEqual(
            power.power_sensor_of(hass, "switch.plug")["entity_id"],
            "sensor.plug_power_factor",
        )

    def test_no_device_or_no_power_sensor_is_none(self):
        hass = _Hass(
            [
                entry("switch.no_device", None),
                entry("switch.plug", "dev"),
                entry("sensor.plug_temperature", "dev"),
            ]
        )
        hass.states.add(
            "sensor.plug_temperature",
            state="24",
            attributes={"device_class": "temperature"},
        )
        self.assertIsNone(power.power_sensor_of(hass, "switch.unknown"))
        self.assertIsNone(power.power_sensor_of(hass, "switch.no_device"))
        self.assertIsNone(power.power_sensor_of(hass, "switch.plug"))


if __name__ == "__main__":
    unittest.main()
