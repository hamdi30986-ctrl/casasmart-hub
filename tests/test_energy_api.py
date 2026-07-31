"""P3 wire-contract tests for Energy Saving REST and lockout responses.

These use the shared real-HA view harness and therefore run in the hub/CI
environment; the HA-free local suite skips them consistently with other views.
"""

from __future__ import annotations

import tempfile
import unittest
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H  # noqa: E402

try:
    from casasmart.api import CasaSmartCommandView
    from casasmart.energy import LEVEL_LOW, EnergyEngine, default_level_config
    from casasmart.energy_api import (
        CasaSmartEnergyActivateView,
        CasaSmartEnergyConfigView,
        CasaSmartEnergyStateView,
    )
    from casasmart.energy_runtime import EnergyFlags
    from casasmart.registry_api import (
        CasaSmartSceneActivateView,
        CasaSmartScenesView,
    )
    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:  # noqa: BLE001 - expected without Home Assistant
    IMPORT_ERROR = err


class _Controller:
    def __init__(self, hass, engine) -> None:
        self._hass = hass
        self.engine = engine

    async def async_state(self):
        return {**self.engine.snapshot(), "issues": [], "stats": self.engine.stats()}

    async def async_activate(
        self, level, *, smart_lockout_enabled=None, actor=None
    ):
        state = self.engine.activate(
            level,
            smart_lockout_enabled=smart_lockout_enabled,
            actor=actor,
        )
        return {"state": state, "apply": {"issues": []}}

    def notify_changed(self):
        pass


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class EnergyApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self.tmp.name)
        self.addCleanup(self.runtime.storage.close)
        self.runtime.energy = EnergyEngine(
            self.runtime.storage.table("energy_configs"),
            self.runtime.storage.table("energy_state"),
            self.runtime.storage.energy_events(),
        )
        self.runtime.energy.warm_up()
        self.runtime.energy_flags = EnergyFlags(
            self.runtime.storage.table("energy_flags")
        )
        self.runtime.energy_controller = _Controller(self.hass, self.runtime.energy)
        _, self.admin = H.session(self.runtime.auth, role="admin")
        _, self.subadmin = H.session(self.runtime.auth, role="sub-admin")
        _, self.user = H.session(self.runtime.auth, role="user")

    async def test_state_is_household_readable_but_config_is_admin_only(self):
        state = CasaSmartEnergyStateView(self.hass)
        status, body = H.read_response(
            await state.get(H.FakeRequest(headers=self.user))
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["active"])
        self.assertNotIn("released_entities", body)
        self.assertNotIn("release_details", body)
        self.assertNotIn("stats", body)

        config = CasaSmartEnergyConfigView(self.hass)
        status, _ = H.read_response(
            await config.get(H.FakeRequest(headers=self.user), LEVEL_LOW)
        )
        self.assertEqual(status, 403)
        status, body = H.read_response(
            await config.get(H.FakeRequest(headers=self.admin), LEVEL_LOW)
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["setup_complete"])

    async def test_incomplete_activation_is_machine_readable_409(self):
        view = CasaSmartEnergyActivateView(self.hass)
        status, body = H.read_response(
            await view.post(
                H.FakeRequest(headers=self.admin, body={"level": LEVEL_LOW})
            )
        )
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "setup_required", "level": LEVEL_LOW})

    async def test_member_device_command_is_blocked_before_payload_execution(self):
        config = default_level_config(LEVEL_LOW)
        config["setup_complete"] = True
        self.runtime.energy.replace_config(LEVEL_LOW, config)
        self.runtime.energy.activate(LEVEL_LOW)
        self.hass.states.add("light.lamp")
        view = CasaSmartCommandView(self.hass)
        with (
            mock.patch("casasmart.api.is_served", return_value=True),
            mock.patch("casasmart.api.in_scope", return_value=True),
        ):
            status, body = H.read_response(
                await view.post(
                    H.FakeRequest(
                        headers=self.user, body={"action": "turn_off"}
                    ),
                    "light.lamp",
                )
            )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "energy_lockout")
        self.assertEqual(self.hass.services.calls, [])

    async def test_scene_gate_distinguishes_locked_role_and_unflagged_scene(self):
        scene = self.runtime.registry.create_scene(
            "Movie", [{"entity_id": "light.lamp", "action": "turn_off", "data": {}}]
        )
        config = default_level_config(LEVEL_LOW)
        config["setup_complete"] = True
        self.runtime.energy.replace_config(LEVEL_LOW, config)
        self.runtime.energy.activate(LEVEL_LOW)
        view = CasaSmartSceneActivateView(self.hass)

        status, body = H.read_response(
            await view.post(H.FakeRequest(headers=self.user), scene["scene_id"])
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "energy_lockout")

        status, body = H.read_response(
            await view.post(H.FakeRequest(headers=self.admin), scene["scene_id"])
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "scene_skipped_energy_saving")

    async def test_subadmin_cannot_set_scene_energy_flag(self):
        view = CasaSmartScenesView(self.hass)
        status, _body = H.read_response(
            await view.post(
                H.FakeRequest(
                    headers=self.subadmin,
                    body={
                        "name": "Movie",
                        "entities": [],
                        "works_during_energy_saving": True,
                    },
                )
            )
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
