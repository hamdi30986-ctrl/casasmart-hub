"""Colour temperature against Home Assistant's real light code.

Both apps work in mireds (``color_temp``, ``min_mireds``, ``max_mireds``);
Home Assistant 2026 accepts and reports kelvin only. These tests drive the hub
with HA's own ``light.turn_on`` schema and a real ``LightEntity``, so they pin
the bridge on whichever release is installed (2025.3 still speaks both).

Runs where Home Assistant is importable (the view harness needs it).
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hastubs import install_casasmart_package, install_homeassistant_stubs

# Install the stubs (a no-op where a real Home Assistant is importable) before
# the harness imports the package.
install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402

try:
    import homeassistant.helpers.config_validation as cv
    from casasmart.api import CasaSmartCommandView
    from casasmart.entity_bridge import serialize_state
    from casasmart.registry_api import CasaSmartSceneActivateView
    from homeassistant.components.light import (
        LIGHT_TURN_ON_SCHEMA,
        ColorMode,
        LightEntity,
    )

    _ERR = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


def _light_state(entity_id: str):
    """The state a real colour-temperature light reports on this HA release."""

    class _Lamp(LightEntity):
        _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        _attr_color_mode = ColorMode.COLOR_TEMP
        _attr_is_on = True
        _attr_brightness = 200
        _attr_color_temp_kelvin = 2700
        _attr_min_color_temp_kelvin = 2000
        _attr_max_color_temp_kelvin = 6535

    lamp = _Lamp()
    attributes = {
        str(key): value
        for key, value in {
            **lamp.capability_attributes,
            **lamp.state_attributes,
        }.items()
    }
    return types.SimpleNamespace(
        entity_id=entity_id,
        domain="light",
        state="on",
        attributes=attributes,
        last_updated=None,
    )


@unittest.skipIf(_SKIP, f"Home Assistant light code unavailable: {_SKIP}")
class LightPayloadTests(unittest.TestCase):
    def test_payload_carries_mireds_on_every_release(self):
        attrs = serialize_state(_light_state("light.lamp"))["attributes"]
        self.assertEqual(attrs["color_temp_kelvin"], 2700)
        self.assertEqual(
            (attrs["color_temp"], attrs["min_mireds"], attrs["max_mireds"]),
            (370, 153, 500),
        )


@unittest.skipIf(_SKIP, f"Home Assistant light code unavailable: {_SKIP}")
class LightCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass.states.add("light.lamp", state="on")
        self.hass.states.add("switch.fan", state="off")
        schema = cv.make_entity_service_schema(LIGHT_TURN_ON_SCHEMA)
        listeners: list = []

        def _listen(_event_type, handler):
            listeners.append(handler)
            return lambda: listeners.remove(handler)

        async def _call(domain, service, data, *, blocking=False):
            # HA validates a light call against the service schema before
            # anything else; anything it rejects raises vol.Invalid here.
            if (domain, service) == ("light", "turn_on"):
                schema(data)
            self.hass.services.calls.append((domain, service, data, blocking))
            for handler in list(listeners):
                handler(types.SimpleNamespace(data={"entity_id": data["entity_id"]}))

        self.hass.bus.async_listen = _listen
        self.hass.services.async_call = _call
        _, self.headers = H.session(self.rt.auth, role="admin")

    async def test_mired_command_reaches_home_assistant_as_kelvin(self) -> None:
        with mock.patch.multiple(
            "casasmart.api",
            is_served=lambda hass, eid: True,
            in_scope=lambda hass, eid, rooms: True,
            serialize_device=lambda hass, state: {"entity_id": state.entity_id},
        ):
            resp = await CasaSmartCommandView(self.hass).post(
                H.FakeRequest(
                    headers=self.headers,
                    body={"action": "turn_on", "data": {"color_temp": 300}},
                ),
                "light.lamp",
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            self.hass.services.calls,
            [
                (
                    "light",
                    "turn_on",
                    {"color_temp_kelvin": 3333, "entity_id": "light.lamp"},
                    True,
                )
            ],
        )

    async def test_saved_mired_scene_step_runs_as_kelvin(self) -> None:
        steps = [
            {
                "entity_id": "light.lamp",
                "action": "turn_on",
                "data": {"brightness_pct": 40, "color_temp": 370},
            },
            {"entity_id": "switch.fan", "action": "turn_on"},
        ]
        scene = self.rt.registry.create_scene("Evening", steps)
        # The stored step keeps the app's mireds: its scene editor reads them.
        self.assertEqual(scene["entities"][0]["data"]["color_temp"], 370)
        with mock.patch(
            "casasmart.registry_api.is_served",
            H.is_served_for(["light.lamp", "switch.fan"]),
        ):
            resp = await CasaSmartSceneActivateView(self.hass).post(
                H.FakeRequest(headers=self.headers), scene["scene_id"]
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(
            self.hass.services.calls[0][2],
            {
                "brightness_pct": 40,
                "color_temp_kelvin": 2703,
                "entity_id": "light.lamp",
            },
        )


if __name__ == "__main__":
    unittest.main()
