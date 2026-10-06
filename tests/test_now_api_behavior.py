"""Behavioral bulk-command tests using tiny Home Assistant boundary fakes."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]


def _module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _load_now_api():
    # The repository deliberately keeps its focused tests dependency-light.  Stub
    # only HA's import boundary, then execute the real bulk command implementation.
    ha = _module("homeassistant", package=True)
    components = _module("homeassistant.components", package=True)
    http = _module("homeassistant.components.http")
    core = _module("homeassistant.core")
    exceptions = _module("homeassistant.exceptions")
    helpers = _module("homeassistant.helpers", package=True)
    entity_registry = _module("homeassistant.helpers.entity_registry")
    ha.components = components
    components.http = http
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.entity_registry = entity_registry

    class View:
        requires_auth = False

        def json(self, value, status=None):
            return value

        def json_message(self, value, status=None):
            return {"message": value, "status": status}

    class HomeAssistantError(Exception):
        pass

    http.HomeAssistantView = View
    core.HomeAssistant = object
    exceptions.HomeAssistantError = HomeAssistantError
    entity_registry.async_get = lambda hass: None

    casa = _module("casasmart", package=True)
    auth = _module("casasmart.auth_api")
    auth.authenticate_request = lambda *args: ({}, None)
    auth.get_engine = lambda hass: None
    auth.json_body = lambda request: None
    const = _module("casasmart.const")
    const.DOMAIN = "casasmart"
    energy = _module("casasmart.energy_runtime")
    energy.energy_lockout_applies = lambda *args: False
    filtering = _module("casasmart.filtering")
    filtering.area_id_of = lambda hass, entity_id: hass.states.get(
        entity_id
    ).attributes.get("room")
    filtering.in_scope = lambda *args: True
    filtering.is_served = lambda *args: True
    filtering.serialize_device = lambda hass, state: {"entity_id": state.entity_id}
    registry = _module("casasmart.registry")
    registry.RegistryEngine = object

    now_spec = importlib.util.spec_from_file_location(
        "casasmart.now_data", ROOT / "custom_components" / "casasmart" / "now_data.py"
    )
    assert now_spec and now_spec.loader
    now_data = importlib.util.module_from_spec(now_spec)
    sys.modules[now_spec.name] = now_data
    now_spec.loader.exec_module(now_data)

    api_spec = importlib.util.spec_from_file_location(
        "casasmart.now_api", ROOT / "custom_components" / "casasmart" / "now_api.py"
    )
    assert api_spec and api_spec.loader
    api = importlib.util.module_from_spec(api_spec)
    sys.modules[api_spec.name] = api
    api_spec.loader.exec_module(api)
    return api, now_data, HomeAssistantError


_API, _NOW, _HOME_ASSISTANT_ERROR = _load_now_api()


class _State:
    def __init__(self, entity_id: str, state: str, room: str) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = {"room": room}


class _States:
    def __init__(self, values: list[_State]) -> None:
        self._values = {value.entity_id: value for value in values}

    def async_all(self):
        return list(self._values.values())

    def get(self, entity_id):
        return self._values.get(entity_id)


class _Services:
    def __init__(self, states: _States, fail_domains=()) -> None:
        self._states = states
        self._fail_domains = set(fail_domains)
        self.calls = []

    async def async_call(self, domain, action, data, blocking=True):
        self.calls.append((domain, action, tuple(data["entity_id"])))
        if domain in self._fail_domains:
            raise _HOME_ASSISTANT_ERROR("simulated service failure")
        target_state = "off" if action == "turn_off" else "on"
        for entity_id in data["entity_id"]:
            self._states.get(entity_id).state = target_state


class _Hass:
    def __init__(self, states: _States, fail_domains=()) -> None:
        self.states = states
        self.services = _Services(states, fail_domains)
        self.config_entries = type(
            "ConfigEntries",
            (),
            {"async_loaded_entries": staticmethod(lambda domain: [])},
        )()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class NowBulkBehaviorTest(unittest.TestCase):
    def test_contact_config_accepts_real_locks_and_rejects_unrelated_domains(
        self,
    ) -> None:
        hass = _Hass(
            _States(
                [
                    _State("lock.gate", "locked", "outdoor"),
                    _State("sensor.gate", "locked", "outdoor"),
                ]
            )
        )
        view = _API.CasaSmartNowConfigView(hass)

        accepted = asyncio.run(
            view._validate_configuration({"contact_entity_ids": ["lock.gate"]})
        )
        rejected = asyncio.run(
            view._validate_configuration({"contact_entity_ids": ["sensor.gate"]})
        )

        self.assertIsNone(accepted)
        self.assertEqual(rejected["status"].value, 400)
        self.assertIn("not a door/window contact", rejected["message"])

    def test_contact_api_counts_unlocked_as_open_and_never_closes_transients(
        self,
    ) -> None:
        hass = _Hass(
            _States(
                [
                    _State("binary_sensor.window", "off", "living"),
                    _State("lock.gate", "unlocked", "outdoor"),
                    _State("lock.side_door", "locked", "outdoor"),
                ]
            )
        )
        view = _API.CasaSmartNowView(hass)

        open_payload = view._contacts_payload(
            ["binary_sensor.window", "lock.gate", "lock.side_door"], None
        )
        self.assertEqual(open_payload["status"], "open")
        self.assertEqual(open_payload["open_count"], 1)
        self.assertEqual(open_payload["unknown_count"], 0)

        for transient in ("locking", "unlocking", "jammed", "unavailable", "unknown"):
            with self.subTest(transient=transient):
                hass.states.get("lock.gate").state = transient
                payload = view._contacts_payload(
                    ["binary_sensor.window", "lock.gate", "lock.side_door"], None
                )
                self.assertEqual(payload["status"], "unknown")
                self.assertEqual(payload["open_count"], 0)
                self.assertEqual(payload["unknown_count"], 1)

    def test_off_captures_only_confirmed_changes_and_on_restores_once(self) -> None:
        states = _States(
            [
                _State("light.kitchen", "on", "room-kitchen"),
                _State("fan.kitchen", "on", "room-kitchen"),
                _State("light.unlisted", "on", "room-kitchen"),
                _State("switch.generic", "on", "room-kitchen"),
                _State("camera.kitchen", "on", "room-kitchen"),
            ]
        )
        hass = _Hass(states, fail_domains={"fan"})
        engine = _NOW.NowDataEngine({}, {}, {}, {}, {})
        engine.set_room_policy("room-kitchen", True, ["light.kitchen", "fan.kitchen"])
        view = _API.CasaSmartRoomActivityCommandView(hass)

        off = asyncio.run(view._run("room-kitchen", "turn_off", engine))
        self.assertFalse(off["ok"])
        self.assertEqual(off["restore_pending_count"], 1)
        self.assertEqual(engine.restore_set("room-kitchen"), ["light.kitchen"])
        self.assertEqual(states.get("light.unlisted").state, "on")
        self.assertEqual(states.get("switch.generic").state, "on")
        self.assertEqual(states.get("camera.kitchen").state, "on")

        hass.services._fail_domains.clear()
        on = asyncio.run(view._run("room-kitchen", "turn_on", engine))
        self.assertTrue(on["ok"])
        self.assertEqual(on["restored_from_capture_count"], 1)
        self.assertEqual(engine.restore_set("room-kitchen"), [])
        self.assertEqual(states.get("light.kitchen").state, "on")


if __name__ == "__main__":
    unittest.main()
