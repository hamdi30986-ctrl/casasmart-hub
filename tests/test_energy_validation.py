"""HA-free P3 tests for wizard discovery/config cross-validation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"),
)

from energy import (  # noqa: E402
    LEVEL_LOW,
    LEVEL_SMART,
    EnergyConfigError,
    default_level_config,
    validate_level_config,
)
from energy_validation import validate_config_against_discovery  # noqa: E402


def _entity(entity_id):
    return {"entity_id": entity_id}


DISCOVERY = {
    "rooms": [
        {
            "room_id": "living",
            "automatic": False,
            "gangs": [
                {
                    "group_id": "wall-3",
                    "channel_count": 3,
                    "channels": [
                        _entity("switch.wall_1"),
                        _entity("switch.wall_2"),
                        _entity("switch.wall_3"),
                    ],
                }
            ],
            "lights": [
                _entity("light.ceiling"),
                _entity("light.lamp"),
                _entity("light.cove"),
            ],
            "plugs": [_entity("switch.tv")],
            "heaters": [_entity("switch.heater")],
            "climates": [_entity("climate.main")],
        },
        {
            "room_id": "hotel",
            "automatic": True,
            "gangs": [],
            "lights": [_entity("light.hotel_1"), _entity("light.hotel_2")],
            "plugs": [],
            "heaters": [],
            "climates": [_entity("climate.hotel")],
        },
    ]
}


class EnergyValidationTests(unittest.TestCase):
    def test_incomplete_wizard_may_persist_valid_partial_steps(self):
        config = default_level_config(LEVEL_LOW)
        config["gang_keepers"] = {
            "wall-3": ["switch.wall_1", "switch.wall_2"]
        }
        validate_config_against_discovery(
            LEVEL_LOW, validate_level_config(LEVEL_LOW, config), DISCOVERY
        )

    def test_completed_low_requires_every_eligible_keeper_group(self):
        config = default_level_config(LEVEL_LOW)
        config.update(
            {
                "gang_keepers": {
                    "wall-3": ["switch.wall_1", "switch.wall_2"]
                },
                "light_keepers": {
                    "living": ["light.ceiling", "light.lamp"],
                    "hotel": ["light.hotel_1"],
                },
                "plug_offs": ["switch.tv"],
                "heaters": [
                    {"entity_id": "switch.heater", "turn_off": True}
                ],
                "setup_complete": True,
            }
        )
        normalized = validate_level_config(LEVEL_LOW, config)
        validate_config_against_discovery(LEVEL_LOW, normalized, DISCOVERY)

        normalized["light_keepers"].pop("living")
        with self.assertRaisesRegex(EnergyConfigError, "light keeper setup"):
            validate_config_against_discovery(LEVEL_LOW, normalized, DISCOVERY)

    def test_stale_or_wrong_count_light_pick_is_rejected(self):
        config = default_level_config(LEVEL_LOW)
        config["light_keepers"] = {"living": ["light.missing"]}
        normalized = validate_level_config(LEVEL_LOW, config)
        with self.assertRaisesRegex(EnergyConfigError, "stale pick"):
            validate_config_against_discovery(LEVEL_LOW, normalized, DISCOVERY)

        normalized["light_keepers"] = {"living": ["light.ceiling"]}
        with self.assertRaisesRegex(EnergyConfigError, "exactly 2"):
            validate_config_against_discovery(LEVEL_LOW, normalized, DISCOVERY)

    def test_smart_automatic_rooms_need_no_static_light_or_ac_pick(self):
        config = default_level_config(LEVEL_SMART)
        config.update(
            {
                "gang_keepers": {"wall-3": ["switch.wall_1"]},
                "light_keepers": {
                    "living": ["light.ceiling", "light.lamp"]
                },
                # Sensorless living room participates; [] means all ACs off.
                "ac_keepers": {"living": []},
                "heaters": [
                    {"entity_id": "switch.heater", "turn_off": False}
                ],
                "setup_complete": True,
            }
        )
        normalized = validate_level_config(LEVEL_SMART, config)
        validate_config_against_discovery(LEVEL_SMART, normalized, DISCOVERY)

    def test_excluded_room_cannot_keep_a_gang_pick(self):
        config = default_level_config(LEVEL_LOW)
        config["excluded_rooms"] = ["living"]
        config["gang_keepers"] = {
            "wall-3": ["switch.wall_1", "switch.wall_2"]
        }
        normalized = validate_level_config(LEVEL_LOW, config)
        with self.assertRaisesRegex(EnergyConfigError, "excluded room"):
            validate_config_against_discovery(LEVEL_LOW, normalized, DISCOVERY)


if __name__ == "__main__":
    unittest.main()
