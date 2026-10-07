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


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class SharedMutationLockTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
