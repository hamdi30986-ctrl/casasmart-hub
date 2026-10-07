"""Execute the real HTTP view and SQLite engine with fake HA boundaries."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import test_atomic_room_moves as fixtures

_REGISTRY = fixtures._REGISTRY
storage_module = fixtures.storage_module


def load_api():
    modules = {}

    def module(name, **values):
        result = ModuleType(name)
        result.__dict__.update(values)
        modules[name] = result
        return result

    class View:
        def json(self, data, status=200):
            return SimpleNamespace(status=int(status), data=data)

        def json_message(self, message, status=200):
            return self.json({"message": message}, status)

    module("homeassistant", __path__=[])
    module("homeassistant.components", __path__=[])
    module("homeassistant.components.http", HomeAssistantView=View)
    module("homeassistant.core", HomeAssistant=object)
    module("homeassistant.exceptions", HomeAssistantError=Exception)
    module("room_api_fixture", __path__=[])
    module(
        "room_api_fixture.auth_api",
        authenticate_request=None,
        get_engine=None,
        read_json_object=None,
    )
    module("room_api_fixture.auth_engine", AuthEngine=object)
    module(
        "room_api_fixture.const",
        DOMAIN="casasmart",
        EVENT_REGISTRY_CHANGED="registry_changed",
    )
    module(
        "room_api_fixture.entity_bridge", CommandError=Exception, validate_command=None
    )
    module("room_api_fixture.energy_runtime", energy_lockout_applies=None)
    module(
        "room_api_fixture.filtering",
        **dict.fromkeys(
            ["area_id_of", "ha_area_id_of", "in_scope", "is_assignable", "is_served"]
        ),
    )
    module("room_api_fixture.runtime_lookup", loaded_runtime_data=None)
    modules["room_api_fixture.registry"] = _REGISTRY
    modules["room_api_fixture.storage"] = storage_module
    path = Path(__file__).parents[1] / "custom_components/casasmart/registry_api.py"
    spec = importlib.util.spec_from_file_location("room_api_fixture.registry_api", path)
    api = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(api)
    return api


class RoomMoveApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.AtomicRoomMoveTest.setUp(self)
        self.api = load_api()
        self.events = []
        self.claims = {"sub": "tablet", "rooms": None}
        self.permissions = []

        async def executor(job):
            return job()

        async def body(view, request):
            # The tests post the payload itself; like read_json_object, refuse
            # anything but a dict.
            if not isinstance(request, dict):
                return None, view.json_message("Body must be a JSON object", 400)
            return request, None

        def authenticate(hass, request, permission):
            self.permissions.append(permission)
            return self.claims, None

        self.hass = SimpleNamespace(
            async_add_executor_job=executor,
            bus=SimpleNamespace(
                async_fire=lambda event, data: self.events.append((event, data))
            ),
        )
        self.api.get_registry = lambda hass: self.engine
        self.api.loaded_runtime_data = lambda hass: SimpleNamespace(storage=self.store)
        self.api.get_engine = lambda hass: SimpleNamespace(
            member_id_for=lambda sub: "member"
        )
        self.api.authenticate_request = authenticate
        self.api.read_json_object = body
        self.api.is_assignable = lambda hass, eid: eid in {"light.one", "cover.two"}
        self.api.ha_area_id_of = lambda hass, eid: None
        self.view = self.api.CasaSmartRoomMoveView(self.hass)

    async def test_success_notifies_after_commit_replay_does_not_notify_again(self):
        response = await self.view.post(self.payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.permissions, ["registry.manage"])
        self.assertEqual(self.engine.room_of("light.one"), "b")
        self.assertEqual(self.events, [("registry_changed", {"kind": "devices"})])
        replay = await self.view.post(self.payload)
        self.assertTrue(replay.data["replayed"])
        self.assertEqual(len(self.events), 1)

    async def test_unauthorized_request_never_writes_or_notifies(self):
        self.api.authenticate_request = lambda *args: (
            None,
            self.view.json_message("denied", 403),
        )
        response = await self.view.post(self.payload)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.engine.room_of("light.one"), "a")
        self.assertEqual(self.events, [])

    async def test_scope_stale_destination_and_conflict_map_to_actionable_status(self):
        self.claims["rooms"] = ["b"]
        self.assertEqual((await self.view.post(self.payload)).status, 403)
        self.claims["rooms"] = None
        self.assertEqual(
            (await self.view.post({**self.payload, "room_id": "gone"})).status, 404
        )
        self.engine.assign_device("light.one", room_id=None)
        response = await self.view.post(self.payload)
        self.assertEqual(response.status, 409)
        self.assertIn("refresh", response.data["message"])
        self.assertEqual(self.events, [])

    async def test_storage_failure_rolls_back_and_does_not_notify(self):
        original = self.store._execute_write

        def fail(sql, params=()):
            if params[:2] == ("devices", "cover.two"):
                raise sqlite3.OperationalError("injected")
            return original(sql, params)

        with patch.object(self.store, "_execute_write", side_effect=fail):
            response = await self.view.post(self.payload)
        self.assertEqual(response.status, 500)
        self.assertEqual(self.engine.room_of("light.one"), "a")
        self.assertEqual(self.events, [])

    async def test_invalid_body_and_missing_runtime_are_rejected(self):
        self.assertEqual((await self.view.post([])).status, 400)
        self.api.loaded_runtime_data = lambda hass: None
        self.assertEqual((await self.view.post(self.payload)).status, 503)
        self.assertEqual(self.events, [])
