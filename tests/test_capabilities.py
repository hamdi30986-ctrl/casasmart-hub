"""Regression tests for the additive Orbit capability handshake contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


_MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "casasmart" / "capabilities.py"
)
_SPEC = importlib.util.spec_from_file_location("casasmart_capabilities", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CAPABILITIES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CAPABILITIES)


class CapabilityContractTest(unittest.TestCase):
    def test_foundation_advertises_all_orbit_features_as_unavailable(self) -> None:
        contract = _CAPABILITIES.handshake_capabilities()

        self.assertEqual(contract["contract_version"], 1)
        self.assertEqual(
            set(contract["features"]),
            {
                "admin_password_v1",
                "room_activity_bulk_v1",
                "now_data_v1",
                "push_relay_optional_v1",
            },
        )
        self.assertTrue(
            all(
                feature
                == {
                    "version": 1,
                    "minimum_api_version": 1,
                    "available": False,
                }
                for feature in contract["features"].values()
            )
        )

    def test_callers_cannot_mutate_future_handshake_responses(self) -> None:
        first = _CAPABILITIES.handshake_capabilities()
        first["features"]["admin_password_v1"]["available"] = True

        fresh = _CAPABILITIES.handshake_capabilities()
        self.assertFalse(fresh["features"]["admin_password_v1"]["available"])


if __name__ == "__main__":
    unittest.main()
