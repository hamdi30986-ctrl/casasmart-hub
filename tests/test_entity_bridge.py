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


class TestStage3aWidening(unittest.TestCase):
    """B16 stage 3a: the dialect the app's sheets actually speak."""

    def test_light_brightness_pct_and_effect(self):
        # lighting_control_sheet sends percent sliders and effect taps.
        _, service, data = validate_command(
            "light.living1", "turn_on", {"brightness_pct": 60}
        )
        self.assertEqual((service, data), ("turn_on", {"brightness_pct": 60}))
        _, _, data = validate_command(
            "light.living1", "turn_on", {"effect": "colorloop", "transition": 1}
        )
        self.assertEqual(data, {"effect": "colorloop", "transition": 1})

    def test_cover_tilt(self):
        _, service, data = validate_command(
            "cover.blinds", "set_tilt_position", {"tilt_position": 45}
        )
        self.assertEqual(service, "set_cover_tilt_position")
        self.assertEqual(data, {"tilt_position": 45})

    def test_select_option(self):
        _, service, data = validate_command(
            "select.sensitivity", "select_option", {"option": "high"}
        )
        self.assertEqual((service, data), ("select_option", {"option": "high"}))

    def test_number_set_value(self):
        _, service, data = validate_command(
            "number.timeout", "set_value", {"value": 12.5}
        )
        self.assertEqual((service, data), ("set_value", {"value": 12.5}))

    def test_siren_on_off_no_data(self):
        _, service, data = validate_command("siren.alarm", "turn_on", {})
        self.assertEqual((service, data), ("turn_on", {}))
        _, service, _ = validate_command("siren.alarm", "turn_off", None)
        self.assertEqual(service, "turn_off")

    def test_light_color_mode_dialect(self):
        # Every color key the sheet's per-bulb command modes emit.
        for key, value in (
            ("hs_color", [210.0, 85.0]),
            ("xy_color", [0.31, 0.32]),
            ("rgbw_color", [255, 200, 120, 0]),
            ("rgbww_color", [255, 200, 120, 0, 0]),
            ("color_temp", 300),
        ):
            with self.subTest(key=key):
                _, service, data = validate_command(
                    "light.living1", "turn_on", {key: value}
                )
                self.assertEqual((service, data), ("turn_on", {key: value}))

    def test_climate_set_temperature_carries_hvac_mode(self):
        # The goodnight quick action preserves the current mode.
        _, service, data = validate_command(
            "climate.bedroom", "set_temperature",
            {"temperature": 22, "hvac_mode": "cool"},
        )
        self.assertEqual(service, "set_temperature")
        self.assertEqual(data, {"temperature": 22, "hvac_mode": "cool"})

    def test_media_player_turn_off(self):
        # The app's "away" quick action powers media players down.
        _, service, data = validate_command("media_player.tv", "turn_off", {})
        self.assertEqual((service, data), ("turn_off", {}))

    def test_media_player_turn_off_rejects_data(self):
        with self.assertRaises(CommandError):
            validate_command("media_player.tv", "turn_off", {"transition": 1})

    def test_siren_rejects_data(self):
        # No tone/duration keys until a sheet actually sends them.
        with self.assertRaises(CommandError):
            validate_command("siren.alarm", "turn_on", {"duration": 600})

    def test_select_rejects_foreign_keys(self):
        with self.assertRaises(CommandError):
            validate_command("select.sensitivity", "select_option", {"entity_id": "x"})

    def test_tilt_rejects_foreign_keys(self):
        with self.assertRaises(CommandError):
            validate_command(
                "cover.blinds", "set_tilt_position", {"position": 5}
            )

    def test_actions_dont_cross_domains(self):
        # number can't borrow select's action and vice versa.
        with self.assertRaises(CommandError):
            validate_command("number.timeout", "select_option", {"option": "x"})
        with self.assertRaises(CommandError):
            validate_command("select.sensitivity", "set_value", {"value": 1})

    def test_new_domains_stay_locked_down(self):
        # Widened ≠ open: anything outside the per-action table still dies.
        with self.assertRaises(CommandError):
            validate_command("number.timeout", "set_min", {"min": 0})
        with self.assertRaises(CommandError):
            validate_command("select.sensitivity", "toggle", {})

    def test_installer_ops_not_exposed(self):
        # automation.reload / mqtt.publish are admin-scope, never app commands.
        self.assertFalse(is_exposed("automation.athan"))
        with self.assertRaises(CommandError):
            validate_command("automation.athan", "reload", {})

    def test_new_attribute_allowlists(self):
        select_attrs = serialize_state(
            FakeState("select.mode", "high", {"options": ["low", "high"], "icon": "x"})
        )["attributes"]
        self.assertEqual(select_attrs, {"options": ["low", "high"]})
        number_attrs = serialize_state(
            FakeState(
                "number.timeout",
                "12",
                {"min": 0, "max": 60, "step": 1, "mode": "slider"},
            )
        )["attributes"]
        self.assertEqual(number_attrs, {"min": 0, "max": 60, "step": 1})

    def test_cover_tilt_attribute_forwarded(self):
        attrs = serialize_state(
            FakeState(
                "cover.blinds",
                "open",
                {"current_position": 80, "current_tilt_position": 45},
            )
        )["attributes"]
        self.assertEqual(attrs["current_tilt_position"], 45)

    def test_light_effect_attributes_forwarded(self):
        attrs = serialize_state(
            FakeState(
                "light.strip",
                "on",
                {"effect": "colorloop", "effect_list": ["colorloop", "fire"]},
            )
        )["attributes"]
        self.assertEqual(attrs["effect"], "colorloop")
        self.assertEqual(attrs["effect_list"], ["colorloop", "fire"])


if __name__ == "__main__":
    unittest.main()
