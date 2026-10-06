"""Regression tests for the additive Orbit capability handshake contract."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "casasmart" / "capabilities.py"
)
_SPEC = importlib.util.spec_from_file_location("casasmart_capabilities", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CAPABILITIES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CAPABILITIES)


class CapabilityContractTest(unittest.TestCase):
    def test_only_complete_now_endpoints_are_advertised(self) -> None:
        contract = _CAPABILITIES.handshake_capabilities()

        self.assertEqual(contract["contract_version"], 1)
        self.assertEqual(
            set(contract["features"]),
            {
                "admin_password_v1",
                "room_activity_bulk_v1",
                "now_data_v1",
                "atomic_room_move_v1",
                "contextual_suggestions_v1",
                "generated_room_suggestions_v1",
                "push_relay_optional_v1",
            },
        )
        self.assertEqual(
            {
                name: feature["available"]
                for name, feature in contract["features"].items()
            },
            {
                "admin_password_v1": False,
                "room_activity_bulk_v1": True,
                "now_data_v1": True,
                "atomic_room_move_v1": True,
                "contextual_suggestions_v1": True,
                "generated_room_suggestions_v1": True,
                "push_relay_optional_v1": False,
            },
        )
        self.assertTrue(
            all(
                feature["version"] == 1 and feature["minimum_api_version"] == 1
                for feature in contract["features"].values()
            )
        )

    def test_callers_cannot_mutate_future_handshake_responses(self) -> None:
        first = _CAPABILITIES.handshake_capabilities()
        first["features"]["now_data_v1"]["available"] = False

        fresh = _CAPABILITIES.handshake_capabilities()
        self.assertTrue(fresh["features"]["now_data_v1"]["available"])


if __name__ == "__main__":
    unittest.main()
