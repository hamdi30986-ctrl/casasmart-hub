"""Duplicate tank provisioning must never rotate a live tank token."""

from __future__ import annotations

import importlib.util
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load_tank_module():
    spec = importlib.util.spec_from_file_location(
        "casasmart_tank_under_test",
        ROOT / "custom_components" / "casasmart" / "tank.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TANK = _load_tank_module()


class _Readings:
    def last(self, device_id: str):
        return None

    def delete_device(self, device_id: str) -> None:
        raise AssertionError(f"duplicate provision mutated readings for {device_id}")


class TankDuplicateTest(unittest.TestCase):
    def test_duplicate_is_normalized_rejected_and_existing_record_unchanged(
        self,
    ) -> None:
        devices: dict[str, dict] = {}
        engine = TANK.TankEngine(devices, _Readings())
        first, _ = engine.mint_device(
            "ShellyPlusUni-AABBCC", "Roof Tank", "192.168.8.59", "SNSN-0043X"
        )
        before = deepcopy(devices)

        with self.assertRaisesRegex(TANK.DuplicateTankError, "already registered"):
            engine.mint_device(
                "  SHELLYPLUSUNI-AABBCC  ",
                "Replacement",
                "192.168.8.99",
                "different-model",
            )

        self.assertEqual(first["device_id"], "shellyplusuni-aabbcc")
        self.assertEqual(devices, before)

    def test_provision_endpoint_maps_duplicate_to_http_conflict(self) -> None:
        source = (ROOT / "custom_components" / "casasmart" / "tank_api.py").read_text()
        self.assertIn("except DuplicateTankError as err:", source)
        self.assertIn("HTTPStatus.CONFLICT", source)


if __name__ == "__main__":
    unittest.main()
