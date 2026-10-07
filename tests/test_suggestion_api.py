"""Real HTTP handlers + scene executor, isolated HA boundaries and SQLite."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import patch

import test_suggestions as fixtures
from test_room_move_api import load_api

# The real cap the auth engine puts on a home-screen widget's token.
WIDGET_SCOPE_PERMISSIONS = importlib.import_module(
    "phase4_fixture.auth_engine"
).WIDGET_SCOPE_PERMISSIONS


def load_boundaries():
    modules = {}

    def module(name, **values):
        result = ModuleType(name)
        result.__dict__.update(values)
        modules[name] = result
        return result

    class View:
        def json(self, data, status=200):
            return NS(data=data, status=int(status))

    def authenticate(hass, request, permission):
        claims = request.claims
        if claims is None:
            return None, NS(
                data={"error": "unauthenticated", "message": "Unauthenticated"},
                status=401,
            )
        if permission == "suggestions.manage" and claims.get("role") != "admin":
            return None, NS(
                data={"error": "forbidden", "message": "Permission denied"}, status=403
            )
        if permission == "devices.control" and not claims.get("control", True):
            return None, NS(
                data={"error": "forbidden", "message": "Permission denied"}, status=403
            )
        if (
            claims.get("scope") == "widget"
            and permission not in WIDGET_SCOPE_PERMISSIONS
        ):
            return None, NS(
                data={"error": "forbidden", "message": "Permission denied"}, status=403
            )
        return claims, None

    async def body(request):
        return request.body

    def track(hass, ids, callback):
        key = object()
        hass.tracked[key] = (set(ids), callback)
        return lambda: hass.tracked.pop(key, None)

    module("homeassistant", __path__=[])
    module("homeassistant.core", callback=lambda f: f)
    module("homeassistant.components", __path__=[])
    module("homeassistant.components.http", HomeAssistantView=View)
    module("homeassistant.helpers", __path__=[])
    module("homeassistant.helpers.event", async_track_state_change_event=track)
    module("homeassistant.helpers.sun", get_astral_event_date=lambda *args: None)
    module(
        "phase4_fixture.filtering",
        area_id_of=lambda h, e: h.rooms.get(e),
        is_served=lambda h, e: e in h.states.values,
        in_scope=lambda h, e, s: s is None or h.rooms.get(e) in s,
    )
    module(
        "phase4_fixture.auth_api",
        authenticate_request=authenticate,
        json_body=body,
        get_engine=lambda h: NS(member_id_for=lambda sub: sub.split(":")[0]),
    )
    module(
        "phase4_fixture.energy_runtime",
        energy_lockout_applies=lambda energy, claims: getattr(energy, "locked", False),
    )
    executor = load_api()
    executor.__package__ = "phase4_fixture"
    spec = importlib.util.spec_from_file_location(
        "phase4_bridge", fixtures.ROOT / "entity_bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    executor.validate_command = bridge.validate_command
    executor.is_served = lambda h, e: e in h.states.values
    executor.area_id_of = lambda h, e: h.rooms.get(e)
    module(
        "phase4_fixture.registry_api",
        async_execute_registry_scene=executor.async_execute_registry_scene,
    )
    loaded = []
    with patch.dict(sys.modules, modules):
        for name in ("suggestion_runtime", "suggestion_api"):
            spec = importlib.util.spec_from_file_location(
                "phase4_fixture." + name, fixtures.ROOT / (name + ".py")
            )
            target = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(target)
            loaded.append(target)
    return loaded


RUNTIME, API = load_boundaries()


class Bus:
    def __init__(self):
        self.events = []
        self.listeners = {}

    def async_listen(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)
        return lambda: self.listeners[event].remove(callback)

    def async_fire(self, event, data=None):
        self.events.append((event, data))
        for cb in list(self.listeners.get(event, [])):
            cb(NS(data=data or {}))


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_errors_keep_common_transport_envelope(self):
        stale = await self.rules.put(
            self.request({"expected_revision": 0, "rules": []})
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.data["error"], "revision_conflict")
        self.assertTrue(stale.data["message"])
        scoped = await self.rules.get(self.request(rooms=["a"]))
        self.assertEqual(scoped.status, 403)
        self.assertTrue(scoped.data["message"])

    async def test_now_snapshot_keeps_static_selection_as_manual_fallback_only(self):
        import test_now_api_behavior as old

        self.data.registry = self.registry
        self.registry.list_rooms = list
        self.registry.get_favorites = lambda member: []
        self.data.now_data = old._NOW.NowDataEngine({}, {}, {}, {}, {})
        self.data.now_data.configure({"suggested_scene_id": "scene-night"})
        now_view = old._API.CasaSmartNowView(self.hass)
        with patch.object(old._API, "authenticate_request", API.authenticate_request):
            configured = await now_view.get(self.request())
            self.assertIsNone(configured["suggested_routine"])
            self.assertIsNone(configured["suggested_routine_source"])
            self.assertEqual(configured["contextual_suggestion"]["status"], "available")
            self.store.replace_rules(1, [])
            legacy = await now_view.get(self.request())
            self.assertEqual(legacy["suggested_routine"]["scene_id"], "scene-night")
            self.assertEqual(legacy["suggested_routine_source"], "featured_manual")
            self.assertEqual(
                legacy["contextual_suggestion"]["status"], "not_configured"
            )
        self.assertEqual(self.calls, [])

    async def test_authorization_rechecked_after_claim_before_dispatch(self):
        oid = (await self.selected())["occurrence_id"]
        request = self.request({"action": "run", "occurrence_id": oid})
        original = self.hass.async_add_executor_job

        async def executor(fn, *args):
            result = await original(fn, *args)
            if fn.__name__ == "claim":
                request.claims["control"] = False
            return result

        self.hass.async_add_executor_job = executor
        self.assertEqual((await self.actions.post(request)).status, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.snapshot()["executions"], {})

    async def test_scene_edited_during_claim_never_executes_old_content(self):
        oid = (await self.selected())["occurrence_id"]
        original = self.hass.async_add_executor_job

        async def executor(fn, *args):
            result = await original(fn, *args)
            if fn.__name__ == "claim":
                self.scenes[0]["name"] = "Edited scene"
            return result

        self.hass.async_add_executor_job = executor
        self.assertEqual((await self.action("run", oid)).status, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.snapshot()["executions"], {})

    async def test_cancelled_run_retains_unknown_receipt_no_retry(self):
        self.gate = asyncio.Event()
        oid = (await self.selected())["occurrence_id"]
        task = asyncio.create_task(self.action("run", oid))
        for _ in range(200):
            if self.calls:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.snapshot()["executions"][oid]["status"], "unknown")
        self.assertEqual((await self.action("run", oid)).data["status"], "unknown")
        self.assertEqual(len(self.calls), 1)

    async def test_disabled_rule_unsubscribes_and_unknown_storage_is_unavailable(self):
        await self.runtime.start()
        self.store.replace_rules(1, [fixtures.rule(enabled=False)])
        await self.runtime.refresh()
        self.assertEqual(self.tracked, {})
        self.store.table["state"] = {"version": 2}
        payload = (await self.view.get(self.request())).data
        self.assertEqual(payload["status"], "unavailable")
        self.assertIsNone(payload["suggestion"])
        self.assertEqual(self.calls, [])

    async def test_burst_updates_debounce_and_payloads_do_not_poll_whole_home(self):
        await self.runtime.start()
        callback = next(iter(self.tracked.values()))[1]
        handles = []
        for _ in range(20):
            callback(None)
            handles.append(self.runtime._debounce)
        self.assertTrue(all(h.cancelled() for h in handles[:-1]))
        self.assertFalse(handles[-1].cancelled())
        self.assertFalse(hasattr(self.states, "async_all"))
        self.assertIsNotNone(await self.selected())
        self.assertEqual(self.calls, [])

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = fixtures.storage.HubStorage(Path(self.tmp.name) / "hub.db")
        self.db.open()
        self.addCleanup(self.db.close)
        self.store = fixtures.stores.SuggestionStore(self.db)
        self.store.replace_rules(0, [fixtures.rule()])
        self.scenes = [fixtures.scene()]
        self.now = datetime(2026, 10, 5, 23, 30, tzinfo=fixtures.UTC)
        self.calls = []
        self.gate = None
        self.fail = set()
        self.tracked = {}
        self.states = NS(
            values={
                "light.one": NS(entity_id="light.one", state="on", attributes={}),
                "switch.private": NS(
                    entity_id="switch.private", state="on", attributes={}
                ),
            }
        )
        self.states.get = lambda eid: self.states.values.get(eid)

        async def call(domain, service, data, blocking=True):
            self.calls.append((domain, service, data))
            if self.gate:
                await self.gate.wait()
            if data["entity_id"] in self.fail:
                raise RuntimeError("offline")
            if service in {"turn_off", "turn_on"}:
                self.states.get(data["entity_id"]).state = (
                    "off" if service == "turn_off" else "on"
                )

        async def executor(fn, *args):
            return await asyncio.to_thread(fn, *args)

        self.hass = NS(
            states=self.states,
            rooms={"light.one": "a", "switch.private": "b"},
            tracked=self.tracked,
            bus=Bus(),
            loop=asyncio.get_running_loop(),
            services=NS(async_call=call),
            config=NS(time_zone="UTC"),
            async_add_executor_job=executor,
            async_create_task=asyncio.create_task,
        )
        self.registry = NS(list_scenes=lambda: deepcopy(self.scenes))
        self.runtime = RUNTIME.SuggestionRuntime(
            self.hass, self.store, self.registry, clock=lambda: self.now
        )
        self.data = NS(suggestions=self.runtime, energy=None)
        self.hass.config_entries = NS(
            async_loaded_entries=lambda domain: [NS(runtime_data=self.data)]
        )
        self.rules = API.CasaSmartSuggestionRulesView(self.hass)
        self.view = API.CasaSmartSuggestionsView(self.hass)
        self.actions = API.CasaSmartSuggestionActionView(self.hass)
        self.preview = API.CasaSmartSuggestionPreviewView(self.hass)
        self.addCleanup(self.runtime.stop)

    def request(self, body=None, **claims):
        return NS(
            body=body,
            claims={"sub": "alice:tablet", "role": "admin", "rooms": None, **claims},
        )

    async def selected(self, **claims):
        result = await self.view.get(self.request(**claims))
        self.assertEqual(result.status, 200)
        return result.data["suggestion"]

    async def action(self, action, occurrence=None, **claims):
        if occurrence is None:
            occurrence = (await self.selected(**claims))["occurrence_id"]
        return await self.actions.post(
            self.request({"action": action, "occurrence_id": occurrence}, **claims)
        )

    async def test_endpoint_contract_and_default_read_only(self):
        self.assertEqual(self.rules.url, "/api/casasmart/now/suggestions/rules")
        self.assertEqual(self.actions.url, "/api/casasmart/now/suggestions/actions")
        self.assertEqual((await self.rules.get(self.request())).data["revision"], 1)
        result = await self.preview.post(self.request({"rule": fixtures.rule()}))
        self.assertTrue(result.data["eligible"])
        selected = await self.selected()
        self.assertEqual(
            set(selected),
            {
                "rule_id",
                "scene_id",
                "scene",
                "occurrence_id",
                "generated_at",
                "expires_at",
                "reason",
            },
        )
        self.assertEqual(selected["reason"]["code"], "time_and_state")
        await self.runtime.start()
        self.assertEqual(self.calls, [])

    async def test_admin_only_management_control_only_execution(self):
        for role in ("user", "sub_admin"):
            self.assertEqual(
                (await self.rules.get(self.request(role=role))).status, 403
            )
            self.assertEqual(
                (
                    await self.rules.put(
                        self.request({"expected_revision": 1, "rules": []}, role=role)
                    )
                ).status,
                403,
            )
            self.assertEqual(
                (
                    await self.preview.post(
                        self.request({"rule": fixtures.rule()}, role=role)
                    )
                ).status,
                403,
            )
        self.assertEqual((await self.rules.get(self.request(rooms=["a"]))).status, 403)
        self.assertEqual((await self.action("run", control=False)).status, 403)
        self.assertEqual((await self.view.get(NS(claims=None))).status, 401)
        self.assertEqual(self.calls, [])

    async def test_full_reference_scope_no_private_names_or_activity(self):
        self.store.replace_rules(
            1,
            [
                fixtures.rule(
                    match="any",
                    conditions=[
                        {"entity_id": "light.one", "state": "on"},
                        {"entity_id": "switch.private", "state": "on"},
                    ],
                )
            ],
        )
        self.assertIsNone(await self.selected(rooms=["a"]))
        self.assertIsNotNone(await self.selected(rooms=["a", "b"]))
        self.assertEqual(self.calls, [])

    async def test_management_revision_validation_disabled_default_and_delete(self):
        raw = fixtures.rule()
        raw.pop("enabled")
        result = await self.rules.put(
            self.request({"expected_revision": 1, "rules": [raw]})
        )
        self.assertEqual(result.status, 200)
        self.assertFalse(result.data["rules"][0]["enabled"])
        self.assertEqual(
            (
                await self.rules.put(
                    self.request({"expected_revision": 1, "rules": []})
                )
            ).status,
            409,
        )
        self.assertEqual(
            (
                await self.rules.put(
                    self.request(
                        {
                            "expected_revision": 2,
                            "rules": [fixtures.rule(scene_id="missing")],
                        }
                    )
                )
            ).status,
            400,
        )
        self.assertEqual(
            (
                await self.rules.put(
                    self.request({"expected_revision": 2, "rules": []})
                )
            ).status,
            200,
        )
        self.assertEqual(
            (await self.view.get(self.request())).data["status"], "not_configured"
        )

    async def test_priority_and_stable_tie_break(self):
        self.store.replace_rules(
            1,
            [
                fixtures.rule(rule_id="z", priority=2),
                fixtures.rule(rule_id="a", priority=2),
                fixtures.rule(rule_id="b", priority=1),
            ],
        )
        self.assertEqual((await self.selected())["rule_id"], "a")

    async def test_per_user_dismiss_and_thirty_minute_snooze(self):
        oid = (await self.selected())["occurrence_id"]
        dismissed = await self.action("dismiss", oid)
        self.assertEqual(dismissed.data["status"], "dismissed")
        self.assertIsNone(await self.selected())
        self.assertIsNone(await self.selected(sub="alice:phone"))
        self.assertIsNotNone(await self.selected(sub="bob:phone"))
        result = await self.action("snooze", oid, sub="bob:phone")
        until = result.data["until"]
        self.now += timedelta(minutes=10)
        self.assertEqual(
            (await self.action("snooze", oid, sub="bob:phone")).data["until"], until
        )
        self.now += timedelta(minutes=20)
        self.assertIsNotNone(await self.selected(sub="bob:phone"))
        self.assertEqual(self.calls, [])

    async def test_widget_token_cannot_dismiss_or_snooze(self):
        # Dismiss/snooze write the person's suggestion state, not a device: a
        # session write. A home-screen widget's token is refused and nothing
        # is suppressed, for the member or anyone else.
        oid = (await self.selected())["occurrence_id"]
        for action in ("dismiss", "snooze"):
            with self.subTest(action=action):
                result = await self.action(action, oid, scope="widget")
                self.assertEqual(result.status, 403)
        self.assertEqual(self.store.snapshot()["suppressions"], {})
        self.assertEqual((await self.selected())["occurrence_id"], oid)
        # Every role's session still dismisses and snoozes (one person each,
        # since a suppression hides the suggestion from that person).
        for role in ("admin", "sub-admin", "user"):
            for action, status in (("snooze", "snoozed"), ("dismiss", "dismissed")):
                with self.subTest(role=role, action=action):
                    sub = f"{role}-{action}:phone"
                    result = await self.action(action, oid, role=role, sub=sub)
                    self.assertEqual(result.data["status"], status)
        self.assertEqual(self.calls, [])

    async def test_widget_token_still_runs_a_suggestion(self):
        # Run activates a scene — device control, which a widget may do.
        oid = (await self.selected())["occurrence_id"]
        result = await self.action("run", oid, scope="widget")
        self.assertEqual(result.status, 200)
        self.assertEqual(len(self.calls), 1)

    async def test_member_lookup_runs_off_the_event_loop(self):
        # Resolving the person behind the token reads SQLite: it belongs in
        # the executor, for the list and the actions alike.
        on_loop = []

        def member_id_for(sub):
            on_loop.append(threading.current_thread() is threading.main_thread())
            return sub.split(":")[0]

        engine = NS(member_id_for=member_id_for)
        with patch.object(API, "get_engine", lambda hass: engine):
            oid = (await self.selected())["occurrence_id"]
            self.assertEqual((await self.action("run", oid)).status, 200)
        self.assertEqual(len(on_loop), 3)  # list, action, re-check before running
        self.assertFalse(any(on_loop), on_loop)

    async def test_success_suppresses_globally_and_replay_does_not_execute(self):
        oid = (await self.selected())["occurrence_id"]
        result = await self.action("run", oid)
        self.assertEqual(result.data["status"], "succeeded")
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(await self.selected(sub="bob:phone"))
        replay = await self.action("run", oid, sub="bob:phone")
        self.assertEqual(replay.data, result.data)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("entity_id", str(result.data))

    async def test_partial_is_visible_and_no_silent_retry(self):
        self.fail.add("light.one")
        oid = (await self.selected())["occurrence_id"]
        result = await self.action("run", oid)
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["status"], "partial_failure")
        self.assertEqual(
            (await self.selected())["last_execution"]["status"], "partial_failure"
        )
        await self.action("run", oid)
        self.assertEqual(len(self.calls), 1)

    async def test_simultaneous_clients_share_durable_execution_claim(self):
        self.gate = asyncio.Event()
        oid = (await self.selected())["occurrence_id"]
        task = asyncio.create_task(self.action("run", oid))
        for _ in range(200):
            if self.calls:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(self.calls), 1)
        duplicate = await self.action("run", oid, sub="bob:phone")
        self.assertEqual(duplicate.status, 202)
        self.assertEqual(duplicate.data["status"], "executing")
        self.gate.set()
        self.assertEqual((await task).data["status"], "succeeded")
        self.assertEqual(len(self.calls), 1)

    async def test_stale_deleted_and_ineligible_occurrences_cannot_run(self):
        oid = (await self.selected())["occurrence_id"]
        self.now += timedelta(hours=7)
        self.assertEqual((await self.action("run", oid)).status, 409)
        self.now -= timedelta(hours=7)
        self.scenes.clear()
        self.assertEqual((await self.action("run", oid)).status, 409)
        self.scenes.append(fixtures.scene())
        self.states.get("light.one").state = "off"
        self.assertEqual((await self.action("run", oid)).status, 409)
        self.assertEqual(self.calls, [])

    async def test_energy_lockout_blocks_run(self):
        self.data.energy = NS(locked=True, active_level="away")
        refused = await self.action("run")
        self.assertEqual(refused.status, 403)
        # The phone keeps "code" on a 403: it tells the lockout apart from a
        # credential that a re-login would fix.
        self.assertEqual(refused.data["error"], "energy_lockout")
        self.assertEqual(refused.data["code"], "energy_lockout")
        self.data.energy.locked = False
        self.assertEqual((await self.action("run")).status, 409)
        self.assertEqual(self.calls, [])

    async def test_lifecycle_replaces_interest_set_and_snooze_expiry_notifies(self):
        await self.runtime.start()
        self.assertEqual(self.runtime._ids, {"light.one"})
        self.assertEqual(len(self.tracked), 1)
        oid = (await self.selected())["occurrence_id"]
        await self.action("snooze", oid)
        before = len(self.hass.bus.events)
        self.now += timedelta(minutes=30)
        await self.runtime.refresh()
        self.assertEqual(len(self.hass.bus.events), before + 1)
        self.assertTrue(all(data is None for _, data in self.hass.bus.events))
        self.store.replace_rules(1, [])
        await self.runtime.refresh()
        self.assertEqual(self.runtime._ids, set())
        self.assertEqual(self.tracked, {})
        self.runtime.stop()
        self.assertTrue(self.runtime._timer.cancelled())

    async def test_time_boundary_and_state_only_refresh_do_not_execute(self):
        self.now = datetime(2026, 10, 5, 22, 59, 59, tzinfo=fixtures.UTC)
        await self.runtime.start()
        self.assertLessEqual(self.runtime._timer.when() - self.hass.loop.time(), 1.1)
        self.assertIsNone(await self.selected())
        self.now += timedelta(seconds=1)
        await self.runtime.refresh()
        self.assertIsNotNone(await self.selected())
        before = len(self.hass.bus.events)
        await self.runtime.refresh()
        self.assertEqual(len(self.hass.bus.events), before)
        self.states.get("light.one").state = "off"
        await self.runtime.refresh()
        self.assertIsNone(await self.selected())
        self.assertEqual(self.calls, [])
