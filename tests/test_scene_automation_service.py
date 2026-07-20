"""CasaSmart registry-scene execution from Home Assistant automations."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H  # noqa: E402

try:
    from casasmart import _async_register_services
    from casasmart.const import DOMAIN

    _ERR = None
except Exception as err:  # noqa: BLE001
    _async_register_services = DOMAIN = None
    _ERR = err


@unittest.skipIf(H.IMPORT_ERROR or _ERR, f"CasaSmart unimportable: {H.IMPORT_ERROR or _ERR}")
class SceneAutomationServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self._tmp.name)
        self.addCleanup(self.runtime.storage.close)

    async def test_registered_service_executes_registry_scene(self) -> None:
        self.hass.states.add("switch.corridor", state="off")
        scene = self.runtime.registry.create_scene(
            "Safe off",
            [{"entity_id": "switch.corridor", "action": "turn_off"}],
        )
        _async_register_services(self.hass)
        handler = self.hass.services.handlers[(DOMAIN, "activate_scene")]

        with mock.patch(
            "casasmart.registry_api.is_served",
            H.is_served_for(["switch.corridor"]),
        ):
            await handler(types.SimpleNamespace(data={"scene_id": scene["scene_id"]}))

        self.assertEqual(
            self.hass.services.calls,
            [("switch", "turn_off", {"entity_id": "switch.corridor"}, True)],
        )

    async def test_service_rejects_unknown_scene(self) -> None:
        _async_register_services(self.hass)
        handler = self.hass.services.handlers[(DOMAIN, "activate_scene")]
        with self.assertRaisesRegex(Exception, "Unknown scene"):
            await handler(types.SimpleNamespace(data={"scene_id": "scene-missing"}))


if __name__ == "__main__":
    unittest.main()
