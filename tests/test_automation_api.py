"""View-layer tests for ``automation_api`` — one writer for automations.yaml.

``build_views`` creates a fresh ``CasaSmartAutomationConfigView`` for HA's own
HTTP app and another for the hub's TLS listener (and again on each daily TLS
refresh). Their read-modify-write of automations.yaml must still be one at a
time, or one request's change is overwritten by another's stale copy.

The view class, automations.yaml and Home Assistant's YAML helpers are real.
``hass`` is a small fake whose executor runs each job inline between two loop
yields: every job is a point where another request may run, as with HA's
thread pool, and the interleaving is the same on every run. Validation, the
entity registry and the auth gate are stubbed; they sit outside the lock.

The same setup pins the light colour temperature bridge: the apps save mireds,
and Home Assistant 2026 runs light actions in kelvin only.

Container/CI only (imports Home Assistant).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart import automation_api
    from homeassistant.util.yaml import load_yaml

    _ERR = None
except Exception as err:
    automation_api = load_yaml = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR

_YAML = """\
- id: casa_automation_a
  alias: Alpha
  triggers: [{trigger: event, event_type: go_a}]
  actions: [{event: ran_a}]
- id: casa_automation_b
  alias: Bravo
  triggers: [{trigger: event, event_type: go_b}]
  actions: [{event: ran_b}]
- id: casa_automation_c
  alias: Charlie
  triggers: [{trigger: event, event_type: go_c}]
  actions: [{event: ran_c}]
"""


def _config(alias: str) -> dict:
    return {
        "alias": alias,
        "triggers": [{"trigger": "event", "event_type": "go"}],
        "actions": [{"event": "ran"}],
    }


class _SteppedHass:
    """The hass surface the view touches, over a real config directory."""

    def __init__(self, config_dir: str) -> None:
        self.config = types.SimpleNamespace(
            path=lambda *parts: os.path.join(config_dir, *parts)
        )
        self.data: dict = {}
        self.services = H.FakeServices()
        self.config_entries = types.SimpleNamespace(
            async_loaded_entries=lambda domain: []
        )

    async def async_add_executor_job(self, func, *args):
        await asyncio.sleep(0)
        result = func(*args)
        await asyncio.sleep(0)
        return result


class _AutomationViewCase(unittest.IsolatedAsyncioTestCase):
    """The real view over a real automations.yaml, with auth stubbed."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "automations.yaml")
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(_YAML)
        self.hass = _SteppedHass(tmp.name)

        async def validate(hass, config_key, config):
            return config

        registry = types.SimpleNamespace(async_get_entity_id=lambda *args: None)
        for patcher in (
            mock.patch.object(
                automation_api,
                "authenticate_request",
                lambda hass, request, permission: ({"sub": "dev-admin"}, None),
            ),
            mock.patch.object(automation_api, "async_validate_config_item", validate),
            mock.patch.object(
                automation_api,
                "er",
                types.SimpleNamespace(async_get=lambda hass: registry),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _view(self):
        return automation_api.CasaSmartAutomationConfigView(self.hass)


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class SharedMutationLockTests(_AutomationViewCase):
    async def _burst(self, plain, tls) -> list[int]:
        """Create, edit and delete at once, spread over two view instances."""
        responses = await asyncio.gather(
            plain.post(
                H.FakeRequest(body=_config("Delta")), config_key="casa_automation_d"
            ),
            tls.post(
                H.FakeRequest(body=_config("Alpha edited")),
                config_key="casa_automation_a",
            ),
            plain.delete(H.FakeRequest(), config_key="casa_automation_b"),
            tls.delete(H.FakeRequest(), config_key="casa_automation_c"),
        )
        return [response.status for response in responses]

    def _stored(self) -> dict[str, str]:
        return {item["id"]: item["alias"] for item in load_yaml(self.path)}

    async def test_two_listeners_keep_every_acknowledged_change(self) -> None:
        # One instance per listener, as build_views creates them.
        statuses = await self._burst(self._view(), self._view())
        self.assertEqual(statuses, [200, 200, 200, 200])
        self.assertEqual(
            self._stored(),
            {"casa_automation_a": "Alpha edited", "casa_automation_d": "Delta"},
        )

    async def test_a_single_instance_still_serializes(self) -> None:
        view = self._view()
        statuses = await self._burst(view, view)
        self.assertEqual(statuses, [200, 200, 200, 200])
        self.assertEqual(
            self._stored(),
            {"casa_automation_a": "Alpha edited", "casa_automation_d": "Delta"},
        )

    # Home Assistant's file helpers report failures as HomeAssistantError
    # (WriteError for a write, a plain HomeAssistantError for unparsable YAML),
    # never as OSError. Each must still get the hub's JSON 500 and leave the
    # file as it was.

    async def test_failed_write_is_a_json_500(self) -> None:
        from homeassistant.util.file import WriteError

        def refuse(path, contents):
            raise WriteError(PermissionError(13, "Permission denied"))

        view = self._view()
        with mock.patch.object(automation_api, "write_utf8_file_atomic", refuse):
            for response in (
                await view.post(
                    H.FakeRequest(body=_config("Delta")),
                    config_key="casa_automation_d",
                ),
                await view.delete(H.FakeRequest(), config_key="casa_automation_a"),
            ):
                status, body = H.read_response(response)
                self.assertEqual(status, 500)
                self.assertEqual(body["message"], "Failed to persist automation")
        self.assertEqual(
            sorted(self._stored()),
            ["casa_automation_a", "casa_automation_b", "casa_automation_c"],
        )

    async def test_overlong_id_is_refused_before_anything_is_written(self) -> None:
        # The Energy Saving flag store refuses ids over 255 characters, so the
        # view must too, before the file changes, not with a 500 afterwards.
        key = "casa_automation_" + "a" * 240
        view = self._view()
        for response in (
            await view.get(H.FakeRequest(), config_key=key),
            await view.post(H.FakeRequest(body=_config("Long")), config_key=key),
            await view.delete(H.FakeRequest(), config_key=key),
        ):
            status, body = H.read_response(response)
            self.assertEqual(status, 400)
            self.assertIn("at most 255 characters", body["message"])
        self.assertEqual(
            sorted(self._stored()),
            ["casa_automation_a", "casa_automation_b", "casa_automation_c"],
        )

    async def test_unparsable_file_is_a_json_500_and_is_not_rewritten(self) -> None:
        broken = "- id: casa_automation_a\n  alias: [unclosed\n"
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(broken)

        view = self._view()
        for response in (
            await view.get(H.FakeRequest(), config_key="casa_automation_a"),
            await view.post(
                H.FakeRequest(body=_config("Delta")), config_key="casa_automation_d"
            ),
            await view.delete(H.FakeRequest(), config_key="casa_automation_a"),
        ):
            status, body = H.read_response(response)
            self.assertEqual(status, 500)
            self.assertIn("automations.yaml unusable", body["message"])
        with open(self.path, encoding="utf-8") as file:
            self.assertEqual(file.read(), broken)


def _light(mireds=None, kelvin=None, **data):
    """A light.turn_on action as the apps' automation editor writes it."""
    if mireds is not None:
        data["color_temp"] = mireds
    if kelvin is not None:
        data["color_temp_kelvin"] = kelvin
    return {
        "action": "light.turn_on",
        "target": {"entity_id": "light.lamp"},
        "data": data,
    }


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class LightColourTemperatureTests(_AutomationViewCase):
    """The apps save light actions in mireds; HA 2026 only runs kelvin."""

    KEY = "casa_automation_20260303_143022015"

    async def _post(self, actions, key="action"):
        response = await self._view().post(
            H.FakeRequest(
                body={
                    "alias": "casa_automation Evening",
                    "trigger": [{"trigger": "time", "at": "19:00:00"}],
                    "condition": [],
                    key: actions,
                    "mode": "single",
                }
            ),
            config_key=self.KEY,
        )
        self.assertEqual(response.status, 200)
        stored = {item["id"]: item for item in load_yaml(self.path)}
        return stored[self.KEY][key]

    async def test_app_light_action_is_stored_in_kelvin(self) -> None:
        import homeassistant.helpers.config_validation as cv
        from homeassistant.components.light import LIGHT_TURN_ON_SCHEMA

        stored = await self._post(
            [_light(370, brightness_pct=50), {"action": "switch.turn_off"}]
        )
        self.assertEqual(
            stored[0]["data"], {"brightness_pct": 50, "color_temp_kelvin": 2703}
        )
        self.assertEqual(stored[1], {"action": "switch.turn_off"})
        # The call HA makes when the automation runs now passes its schema.
        cv.make_entity_service_schema(LIGHT_TURN_ON_SCHEMA)(
            {"entity_id": "light.lamp", **stored[0]["data"]}
        )

    async def test_nested_actions_are_converted_and_kelvin_wins(self) -> None:
        stored = await self._post(
            [
                {
                    "choose": [{"conditions": [], "sequence": [_light(300)]}],
                    "default": [_light(250)],
                },
                {"if": [], "then": [_light(400)], "else": [_light(500)]},
                {"parallel": [_light(153), {"sequence": [_light(454)]}]},
                {"repeat": {"count": 2, "sequence": [_light(370)]}},
                _light(370, kelvin=3000),
                _light("{{ states('input_number.mireds') }}"),
            ],
            key="actions",
        )
        kelvins = [
            stored[0]["choose"][0]["sequence"][0]["data"],
            stored[0]["default"][0]["data"],
            stored[1]["then"][0]["data"],
            stored[1]["else"][0]["data"],
            stored[2]["parallel"][0]["data"],
            stored[2]["parallel"][1]["sequence"][0]["data"],
            stored[3]["repeat"]["sequence"][0]["data"],
            stored[4]["data"],
        ]
        self.assertEqual(
            kelvins,
            [
                {"color_temp_kelvin": kelvin}
                for kelvin in (3333, 4000, 2500, 2000, 6536, 2203, 2703, 3000)
            ],
        )
        # A template isn't a number of mireds; HA reports it when it runs.
        self.assertEqual(
            stored[5]["data"], {"color_temp": "{{ states('input_number.mireds') }}"}
        )

    async def test_editor_reads_back_the_mireds_it_saved(self) -> None:
        await self._post([_light(370)])
        response = await self._view().get(H.FakeRequest(), config_key=self.KEY)
        status, body = H.read_response(response)
        self.assertEqual(status, 200)
        self.assertEqual(
            body["action"][0]["data"], {"color_temp_kelvin": 2703, "color_temp": 370}
        )


_HA_EDITOR_YAML = """\
- id: casa_automation_e
  alias: Evening
  description: ''
  triggers:
  - trigger: sun
    event: sunset
    offset: 0
  conditions:
  - condition: state
    entity_id: binary_sensor.door
    state: 'off'
  actions:
  - action: light.turn_on
    target:
      entity_id: light.lamp
    data:
      color_temp_kelvin: 2700
  mode: single
"""


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class HaEditorConfigTests(_AutomationViewCase):
    """HA 2024.10+ saves plural keys; the apps parse the singular ones."""

    async def test_plural_keys_read_as_singular_and_the_file_is_untouched(self):
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(_HA_EDITOR_YAML)
        response = await self._view().get(
            H.FakeRequest(), config_key="casa_automation_e"
        )
        status, body = H.read_response(response)
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "id": "casa_automation_e",
                "alias": "Evening",
                "description": "",
                "trigger": [{"trigger": "sun", "event": "sunset", "offset": 0}],
                "condition": [
                    {
                        "condition": "state",
                        "entity_id": "binary_sensor.door",
                        "state": "off",
                    }
                ],
                "action": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": "light.lamp"},
                        "data": {"color_temp_kelvin": 2700, "color_temp": 370},
                    }
                ],
                "mode": "single",
                "works_during_energy_saving": False,
            },
        )
        with open(self.path, encoding="utf-8") as file:
            self.assertEqual(file.read(), _HA_EDITOR_YAML)


if __name__ == "__main__":
    unittest.main()
