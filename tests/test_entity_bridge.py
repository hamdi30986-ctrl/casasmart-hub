"""Unit tests for the B1.4 entity bridge (stdlib unittest, no dependencies).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from entity_bridge import (  # noqa: E402
    CommandError,
    EXPOSED_DOMAINS,
    READ_ONLY_DOMAINS,
    entity_domain,
    is_exposed,
    serialize_state,
    validate_command,
)


class FakeState:
    """Duck-typed stand-in for homeassistant.core.State."""

    def __init__(self, entity_id, state, attributes=None, last_updated=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = last_updated


class TestExposure(unittest.TestCase):
    def test_entity_domain(self):
        self.assertEqual(entity_domain("light.living1"), "light")
        self.assertEqual(entity_domain("binary_sensor.smoke_kitchen"), "binary_sensor")

    def test_exposed_domains(self):
        self.assertTrue(is_exposed("light.living1"))
        self.assertTrue(is_exposed("climate.bedroom"))
        self.assertFalse(is_exposed("automation.athan"))
        self.assertFalse(is_exposed("persistent_notification.x"))
        self.assertFalse(is_exposed("update.core"))

    def test_read_only_domains_are_exposed(self):
        # Read-only still means visible — just not commandable.
        self.assertTrue(READ_ONLY_DOMAINS <= EXPOSED_DOMAINS)


class TestSerializeState(unittest.TestCase):
    def test_basic_shape(self):
        ts = datetime(2026, 6, 10, 3, 0, 0, tzinfo=timezone.utc)
        state = FakeState(
            "light.living1",
            "on",
            {"friendly_name": "Living 1", "brightness": 200, "icon": "mdi:bulb"},
            last_updated=ts,
        )
        device = serialize_state(state, area="Living Room")
        self.assertEqual(device["entity_id"], "light.living1")
        self.assertEqual(device["name"], "Living 1")
        self.assertEqual(device["domain"], "light")
        self.assertEqual(device["state"], "on")
        self.assertEqual(device["area"], "Living Room")
        self.assertEqual(device["last_updated"], ts.isoformat())

    def test_attribute_allowlist_filters(self):
        state = FakeState(
            "light.living1",
            "on",
            {
                "brightness": 200,
                "rgb_color": [255, 0, 0],
                "icon": "mdi:bulb",  # not allowlisted
                "restored": True,  # not allowlisted
                "supported_features": 63,  # not allowlisted
            },
        )
        attrs = serialize_state(state)["attributes"]
        self.assertEqual(
            attrs, {"brightness": 200, "rgb_color": [255, 0, 0]}
        )

    def test_name_falls_back_to_entity_id(self):
        device = serialize_state(FakeState("switch.plug", "off"))
        self.assertEqual(device["name"], "switch.plug")
        self.assertIsNone(device["area"])
        self.assertIsNone(device["last_updated"])

    def test_sensor_attributes(self):
        state = FakeState(
            "sensor.kitchen_temp",
            "28.1",
            {"unit_of_measurement": "°C", "device_class": "temperature", "battery": 90},
        )
        attrs = serialize_state(state)["attributes"]
        self.assertEqual(
            attrs, {"unit_of_measurement": "°C", "device_class": "temperature"}
        )


class TestValidateCommand(unittest.TestCase):
    def test_light_turn_on_with_data(self):
        domain, service, data = validate_command(
            "light.living1", "turn_on", {"brightness": 128}
        )
        self.assertEqual((domain, service), ("light", "turn_on"))
        self.assertEqual(data, {"brightness": 128})

    def test_action_without_data(self):
        domain, service, data = validate_command("lock.front_door", "unlock", None)
        self.assertEqual((domain, service), ("lock", "unlock"))
        self.assertEqual(data, {})

    def test_cover_maps_to_ha_service_names(self):
        _, service, _ = validate_command("cover.2nd_floor_curtains", "open", {})
        self.assertEqual(service, "open_cover")

    def test_rejects_unexposed_domain(self):
        with self.assertRaises(CommandError):
            validate_command("automation.athan", "turn_off", {})

    def test_rejects_read_only_domain(self):
        with self.assertRaises(CommandError):
            validate_command("sensor.kitchen_temp", "turn_off", {})

    def test_rejects_unknown_action(self):
        with self.assertRaises(CommandError):
            validate_command("light.living1", "self_destruct", {})

    def test_rejects_unlisted_data_keys(self):
        with self.assertRaises(CommandError):
            validate_command("light.living1", "turn_off", {"brightness": 0})

    def test_rejects_bad_action_type(self):
        with self.assertRaises(CommandError):
            validate_command("light.living1", None, {})
        with self.assertRaises(CommandError):
            validate_command("light.living1", "", {})

    def test_rejects_bad_data_type(self):
        with self.assertRaises(CommandError):
            validate_command("light.living1", "turn_on", "brightness=200")

    def test_climate_set_temperature(self):
        domain, service, data = validate_command(
            "climate.bedroom", "set_temperature", {"temperature": 22}
        )
        self.assertEqual((domain, service), ("climate", "set_temperature"))
        self.assertEqual(data, {"temperature": 22})


if __name__ == "__main__":
    unittest.main()
