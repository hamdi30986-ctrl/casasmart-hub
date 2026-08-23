"""Regression checks for the additive REST surface without HA test fixtures."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class NowEndpointContractTest(unittest.TestCase):
    def test_authenticated_snapshot_and_bulk_views_are_registered(self) -> None:
        source = (ROOT / "custom_components" / "casasmart" / "now_api.py").read_text()
        api = (ROOT / "custom_components" / "casasmart" / "api.py").read_text()

        for path in (
            '/api/{DOMAIN}/now',
            '/api/{DOMAIN}/now/config',
            '/api/{DOMAIN}/now/rooms/{{room_id}}/activity-policy',
            '/api/{DOMAIN}/now/rooms/{{room_id}}/activity',
        ):
            self.assertIn(path, source)
        self.assertIn('authenticate_request(self._hass, request, "devices.read")', source)
        self.assertIn('authenticate_request(self._hass, request, "devices.control")', source)
        self.assertIn('authenticate_request(self._hass, request, "registry.manage")', source)
        for view in (
            "CasaSmartNowView(hass)",
            "CasaSmartNowConfigView(hass)",
            "CasaSmartRoomActivityPolicyView(hass)",
            "CasaSmartRoomActivityCommandView(hass)",
        ):
            self.assertIn(view, api)

    def test_bulk_contract_uses_idempotency_and_hub_owned_restore_state(self) -> None:
        source = (ROOT / "custom_components" / "casasmart" / "now_api.py").read_text()
        self.assertIn("validate_idempotency_key", source)
        self.assertIn("idempotent_result", source)
        self.assertIn("save_idempotent_result", source)
        self.assertIn("save_restore_set", source)
        self.assertIn("consume_restore_set", source)
        self.assertIn("energy_lockout_applies", source)


if __name__ == "__main__":
    unittest.main()
