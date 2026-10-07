"""Behavioral bulk-command tests using tiny Home Assistant boundary fakes."""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

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

    _module("casasmart", package=True)
    auth = _module("casasmart.auth_api")
    auth.authenticate_request = lambda *args: ({}, None)

    async def async_member_id(hass, claims):
        return claims["sub"]

    auth.async_member_id = async_member_id
    auth.read_json_object = None

    def ready_or_503(view, engine):
        if engine is None:
            return None, view.json_message("Hub not ready", 503)
        return engine, None

    auth.ready_or_503 = ready_or_503
    const = _module("casasmart.const")
    const.DOMAIN = "casasmart"
    energy = _module("casasmart.energy_runtime")
    energy.energy_lockout_applies = lambda *args: False
    energy.energy_lockout_refusal = dict
    filtering = _module("casasmart.filtering")
    filtering.area_id_of = lambda hass, entity_id: hass.states.get(
        entity_id
    ).attributes.get("room")
    filtering.in_scope = lambda *args: True
    filtering.is_served = lambda *args: True
    filtering.serialize_device = lambda hass, state: {"entity_id": state.entity_id}
    registry = _module("casasmart.registry")
    registry.RegistryEngine = object
    storage = _module("casasmart.storage")
    storage.StorageError = type("StorageError", (Exception,), {})

    lookup_spec = importlib.util.spec_from_file_location(
        "casasmart.runtime_lookup",
        ROOT / "custom_components" / "casasmart" / "runtime_lookup.py",
    )
    assert lookup_spec and lookup_spec.loader
    lookup = importlib.util.module_from_spec(lookup_spec)
    sys.modules[lookup_spec.name] = lookup
    lookup_spec.loader.exec_module(lookup)

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


# The stub homeassistant/casasmart modules exist only while loading, so they
# never shadow the real packages the other suites import.
with patch.dict(sys.modules):
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
        self.data: dict = {}
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


class RepeatedRoomOffTest(unittest.TestCase):
    """Room commands through the real POST: idempotency, room lock and _run."""

    ROOM = "room-kitchen"
    ENTITIES = ("light.a", "light.b", "fan.c")

    def setUp(self) -> None:
        self.states = _States(
            [
                _State("light.a", "on", self.ROOM),
                _State("light.b", "on", self.ROOM),
                _State("fan.c", "off", self.ROOM),
            ]
        )
        self.hass = _Hass(self.states)
        self.engine = _NOW.NowDataEngine({}, {}, {}, {}, {})
        self.engine.set_room_policy(self.ROOM, True, list(self.ENTITIES))
        self.view = _API.CasaSmartRoomActivityCommandView(self.hass)

    def command(self, action: str, key: str, member: str = "member-a") -> dict:
        async def body(view, request):
            return {"action": action, "idempotency_key": key}, None

        async def accessible(room_id, scope):
            return True

        with (
            patch.multiple(
                _API,
                authenticate_request=lambda *args: ({"sub": member}, None),
                read_json_object=body,
                get_now_data=lambda hass: self.engine,
            ),
            patch.object(self.view, "_room_accessible", accessible),
        ):
            return asyncio.run(self.view.post(None, self.ROOM))

    def state_of(self) -> dict[str, str]:
        return {
            entity_id: self.states.get(entity_id).state for entity_id in self.ENTITIES
        }

    def test_off_off_on_from_two_members_restores_everything(self) -> None:
        self.command("turn_off", "off-member-a-1", member="member-a")
        second = self.command("turn_off", "off-member-b-1", member="member-b")
        self.assertEqual(second["outcomes"], [])
        self.assertEqual(self.engine.restore_set(self.ROOM), ["light.a", "light.b"])
        self.assertEqual(second["restore_pending_count"], 2)

        on = self.command("turn_on", "on-member-a-1", member="member-a")
        self.assertEqual(on["restored_from_capture_count"], 2)
        self.assertEqual(
            self.state_of(), {"light.a": "on", "light.b": "on", "fan.c": "off"}
        )
        self.assertEqual(self.engine.restore_set(self.ROOM), [])

    def test_devices_switched_on_between_offs_join_the_capture(self) -> None:
        self.command("turn_off", "off-key-0001")
        # Someone switches a captured light and an uncaptured fan back on.
        self.states.get("light.b").state = "on"
        self.states.get("fan.c").state = "on"
        second = self.command("turn_off", "off-key-0002")
        self.assertEqual(
            [item["entity_id"] for item in second["outcomes"]], ["light.b", "fan.c"]
        )
        # First-captured order, each entity once.
        self.assertEqual(
            self.engine.restore_set(self.ROOM), ["light.a", "light.b", "fan.c"]
        )
        self.assertEqual(second["restore_pending_count"], 3)

        on = self.command("turn_on", "on-key-0001")
        self.assertEqual(on["restored_from_capture_count"], 3)
        self.assertEqual(
            self.state_of(), {"light.a": "on", "light.b": "on", "fan.c": "on"}
        )

    def test_identical_key_retry_is_replayed_not_rerun(self) -> None:
        first = self.command("turn_off", "off-key-0001")
        calls = list(self.hass.services.calls)
        self.assertEqual(self.command("turn_off", "off-key-0001"), first)
        self.assertEqual(self.hass.services.calls, calls)
        self.assertEqual(self.engine.restore_set(self.ROOM), ["light.a", "light.b"])

        self.command("turn_on", "on-key-0001")
        # A delayed retry of the old OFF replays its stored answer: it neither
        # switches the restored lights off again nor re-captures them.
        self.assertEqual(self.command("turn_off", "off-key-0001"), first)
        self.assertEqual(
            self.state_of(), {"light.a": "on", "light.b": "on", "fan.c": "off"}
        )
        self.assertEqual(self.engine.restore_set(self.ROOM), [])


class _ExecutorTrackingHass(_Hass):
    """Records whether a function runs as an executor job."""

    def __init__(self, states: _States, runtime=None) -> None:
        super().__init__(states)
        self.in_executor = False
        if runtime is not None:
            self.config_entries = SimpleNamespace(
                async_loaded_entries=lambda domain: [
                    SimpleNamespace(runtime_data=runtime)
                ]
            )

    async def async_add_executor_job(self, func, *args):
        self.in_executor = True
        try:
            return func(*args)
        finally:
            self.in_executor = False


class MemberLookupTest(unittest.TestCase):
    """Who sent a request is a storage read (auth_api.async_member_id runs it
    in the executor); a storage error there is a clean 500 before anything is
    switched."""

    ROOM = "room-kitchen"

    def setUp(self) -> None:
        self.states = _States([_State("light.a", "on", self.ROOM)])
        self.engine = _NOW.NowDataEngine({}, {}, {}, {}, {})
        self.engine.set_room_policy(self.ROOM, True, ["light.a"])
        self.runtime = SimpleNamespace(
            now_data=self.engine,
            energy=None,
            registry=SimpleNamespace(
                list_rooms=list, list_scenes=list, get_favorites=lambda member: []
            ),
        )
        self.hass = _ExecutorTrackingHass(self.states, self.runtime)
        self.lookups: list[bool] = []

    def _lookup(self, sub: str) -> str:
        self.lookups.append(self.hass.in_executor)
        return "member-a"

    @staticmethod
    def _unavailable(sub: str) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    def _patched(self, lookup):
        async def body(view, request):
            return {"action": "turn_off", "idempotency_key": "off-key-0001"}, None

        async def member(hass, claims):
            return await hass.async_add_executor_job(lookup, claims["sub"])

        return patch.multiple(
            _API,
            authenticate_request=lambda *args: ({"sub": "dev-1"}, None),
            read_json_object=body,
            get_now_data=lambda hass: self.engine,
            async_member_id=member,
        )

    def _room_command(self, lookup):
        view = _API.CasaSmartRoomActivityCommandView(self.hass)

        async def accessible(room_id, scope):
            return True

        with self._patched(lookup), patch.object(view, "_room_accessible", accessible):
            return asyncio.run(view.post(None, self.ROOM))

    def _snapshot(self, lookup):
        with self._patched(lookup):
            return asyncio.run(_API.CasaSmartNowView(self.hass).get(None))

    def test_room_command_looks_up_the_member_in_the_executor(self) -> None:
        result = self._room_command(self._lookup)
        self.assertEqual(self.lookups, [True])
        self.assertEqual([o["entity_id"] for o in result["outcomes"]], ["light.a"])

    def test_energy_lockout_refuses_before_switching_anything(self) -> None:
        refusal = {"error": "energy_lockout", "code": "energy_lockout"}
        self.runtime.energy = object()
        with patch.multiple(
            _API,
            energy_lockout_applies=lambda *args: True,
            energy_lockout_refusal=lambda: refusal,
        ):
            result = self._room_command(self._lookup)
        self.assertEqual(result, refusal)
        self.assertEqual(self.hass.services.calls, [])

    def test_room_command_storage_error_is_a_clean_500(self) -> None:
        result = self._room_command(self._unavailable)
        self.assertEqual(result, {"message": "Storage failure", "status": 500})
        self.assertEqual(self.hass.services.calls, [])

    def test_now_snapshot_looks_up_the_member_in_the_executor(self) -> None:
        self._snapshot(self._lookup)
        self.assertEqual(self.lookups, [True])

    def test_now_snapshot_storage_error_is_a_clean_500(self) -> None:
        result = self._snapshot(self._unavailable)
        self.assertEqual(result, {"message": "Storage failure", "status": 500})


class NowConfigSceneValidationTest(unittest.TestCase):
    """PUT /now/config answers malformed scene fields with 400, never 500."""

    def put(self, payload: dict) -> dict:
        engine = _NOW.NowDataEngine({}, {}, {}, {}, {})
        registry = type(
            "Registry", (), {"list_scenes": lambda self: [{"scene_id": "scene-a"}]}
        )()
        view = _API.CasaSmartNowConfigView(_Hass(_States([])))

        async def body(view, request):
            return payload, None

        with (
            patch.multiple(
                _API,
                authenticate_request=lambda *args: ({"sub": "admin"}, None),
                read_json_object=body,
                get_now_data=lambda hass: engine,
            ),
            patch.object(view, "_registry", lambda: registry),
        ):
            return asyncio.run(view.put(None))

    def test_known_scenes_are_accepted(self) -> None:
        stored = self.put(
            {"suggested_scene_id": "scene-a", "pinned_scene_ids": ["scene-a"]}
        )
        self.assertEqual(stored["suggested_scene_id"], "scene-a")
        self.assertEqual(stored["pinned_scene_ids"], ["scene-a"])

    def test_malformed_scene_fields_are_bad_requests(self) -> None:
        for payload in (
            {"suggested_scene_id": ["scene-a"]},
            {"suggested_scene_id": {"id": "scene-a"}},
            {"pinned_scene_ids": 5},
            {"pinned_scene_ids": [["scene-a"]]},
            {"pinned_scene_ids": "scene-a"},
            {"pinned_scene_ids": ["scene-missing"]},
        ):
            with self.subTest(payload=payload):
                response = self.put(payload)
                self.assertEqual(response["status"].value, 400)


class _SteppedServices(_Services):
    async def async_call(self, domain, action, data, blocking=True):
        await asyncio.sleep(0)
        await super().async_call(domain, action, data, blocking)
        await asyncio.sleep(0)


class _SteppedHass(_Hass):
    """Executor jobs and service calls yield to the loop around their work, as
    HA's thread pool and blocking service calls do, so the interleaving is the
    same on every run. ``trace`` names the command task behind each job."""

    def __init__(self, states: _States) -> None:
        super().__init__(states)
        self.services = _SteppedServices(states)
        self.trace: list[str] = []

    async def async_add_executor_job(self, func, *args):
        self.trace.append(asyncio.current_task().get_name())
        await asyncio.sleep(0)
        result = func(*args)
        await asyncio.sleep(0)
        return result


def _turns(trace: list[str]) -> list[str]:
    """Collapse consecutive jobs of one command: ["off", "on"] is serial."""
    return [name for i, name in enumerate(trace) if i == 0 or trace[i - 1] != name]


class RoomCommandLockTest(unittest.IsolatedAsyncioTestCase):
    """Commands for one room wait for each other across view instances."""

    async def asyncSetUp(self) -> None:
        self.states = _States(
            [
                _State("light.a", "on", "room-k"),
                _State("light.b", "on", "room-k"),
                _State("light.c", "on", "room-l"),
            ]
        )
        self.hass = _SteppedHass(self.states)
        self.engine = _NOW.NowDataEngine({}, {}, {}, {}, {})
        self.engine.set_room_policy("room-k", True, ["light.a", "light.b"])
        self.engine.set_room_policy("room-l", True, ["light.c"])

        async def body(view, request):
            return request, None  # the tests post the JSON payload itself

        async def accessible(self, room_id, scope):
            return True

        for patcher in (
            patch.multiple(
                _API,
                authenticate_request=lambda *args: ({"sub": "member-a"}, None),
                read_json_object=body,
                get_now_data=lambda hass: self.engine,
            ),
            patch.object(
                _API.CasaSmartRoomActivityCommandView, "_room_accessible", accessible
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _view(self):
        return _API.CasaSmartRoomActivityCommandView(self.hass)

    async def _race(self, *commands) -> list[dict]:
        tasks = [
            asyncio.create_task(
                view.post({"action": action, "idempotency_key": key}, room),
                name=name,
            )
            for name, view, room, action, key in commands
        ]
        return await asyncio.gather(*tasks)

    async def test_off_and_on_for_one_room_serialize_across_listeners(self) -> None:
        # One instance per listener, as build_views creates them.
        off, on = await self._race(
            ("off", self._view(), "room-k", "turn_off", "off-key-0001"),
            ("on", self._view(), "room-k", "turn_on", "on-key-0001"),
        )
        self.assertEqual(_turns(self.hass.trace), ["off", "on"])
        # ON saw the capture OFF made, restored it, and only then cleared it.
        self.assertEqual(off["restore_pending_count"], 2)
        self.assertEqual(on["restored_from_capture_count"], 2)
        self.assertEqual(self.states.get("light.a").state, "on")
        self.assertEqual(self.states.get("light.b").state, "on")
        self.assertEqual(self.engine.restore_set("room-k"), [])

    async def test_different_rooms_run_independently(self) -> None:
        # Even through one view, so through the one shared set of room locks.
        view = self._view()
        kitchen, lounge = await self._race(
            ("kitchen", view, "room-k", "turn_off", "off-key-0001"),
            ("lounge", view, "room-l", "turn_off", "off-key-0002"),
        )
        self.assertGreater(len(_turns(self.hass.trace)), 2)  # interleaved
        self.assertEqual(kitchen["restore_pending_count"], 2)
        self.assertEqual(lounge["restore_pending_count"], 1)
        self.assertEqual(self.engine.restore_set("room-k"), ["light.a", "light.b"])
        self.assertEqual(self.engine.restore_set("room-l"), ["light.c"])


if __name__ == "__main__":
    unittest.main()
