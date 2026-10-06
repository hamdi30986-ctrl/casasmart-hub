"""Cover controls must receive explicit capabilities without widening other data."""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "cover_bridge_fixture",
    Path(__file__).parents[1] / "custom_components/casasmart/entity_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class CoverCapabilitiesTest(unittest.TestCase):
    def serialize(self, entity_id="cover.blind", **attributes):
        return bridge.serialize_state(
            SimpleNamespace(
                entity_id=entity_id,
                state="open",
                attributes=attributes,
            )
        )["attributes"]

    def test_position_and_tilt_capabilities_survive_serialization(self):
        for mask in (0, 3, 15, 128, 143):
            with self.subTest(mask=mask):
                self.assertEqual(
                    self.serialize(
                        supported_features=mask,
                        current_position=50,
                        current_tilt_position=25,
                        device_class="blind",
                        private_debug_data="not exposed",
                    ),
                    {
                        "supported_features": mask,
                        "current_position": 50,
                        "current_tilt_position": 25,
                        "device_class": "blind",
                    },
                )

    def test_unknown_position_and_legacy_capabilities_are_not_invented(self):
        self.assertEqual(
            self.serialize(supported_features=4), {"supported_features": 4}
        )
        self.assertEqual(self.serialize(current_position=50), {"current_position": 50})

    def test_change_is_cover_scoped(self):
        self.assertEqual(self.serialize("switch.one", supported_features=143), {})


if __name__ == "__main__":
    unittest.main()
