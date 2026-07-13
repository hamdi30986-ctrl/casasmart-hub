"""Unit tests for B8 Piece 4: the hub push dispatcher.

The dispatcher is Home Assistant glue, so (like the alarm adapter) it imports
``homeassistant.*`` at module top. HA isn't installed in the test env, so light
stubs go into ``sys.modules`` before the import. Everything else is REAL: the
``PushSigner`` (real Ed25519), the ``PushTokenStore`` over a temp DB, and the
exact canonical-JSON signing path — so a signature captured here verifies with
the public key, proving the wire contract end to end. ``hass`` and the aiohttp
session are hand-rolled fakes.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))


# -- homeassistant stubs (installed before importing the dispatcher) ----------
def _install_ha_stubs() -> None:
    """Augment (never clobber) the HA stub modules.

    Under ``unittest discover`` another suite (e.g. test_alarm_adapter) may have
    already installed ``homeassistant`` with a DIFFERENT subset of symbols. So
    this is additive: it always ensures ``const`` has the lock states the
    dispatcher imports and ``core`` has Event/HomeAssistant/callback, without
    removing whatever a sibling suite needs.
    """
    ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    assert ha is sys.modules["homeassistant"]

    const = sys.modules.setdefault(
        "homeassistant.const", types.ModuleType("homeassistant.const")
    )
    const.STATE_LOCKED = "locked"
    const.STATE_UNLOCKED = "unlocked"
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"

    core = sys.modules.setdefault(
        "homeassistant.core", types.ModuleType("homeassistant.core")
    )
    if not hasattr(core, "Event"):

        class Event:  # only ``.data`` is touched
            def __init__(self, data):
                self.data = data

        core.Event = Event
    if not hasattr(core, "HomeAssistant"):

        class HomeAssistant:  # never instantiated — FakeHass duck-types it
            pass

        core.HomeAssistant = HomeAssistant
    if not hasattr(core, "callback"):

        def callback(fn):  # the @callback decorator is a no-op marker in HA
            return fn

        core.callback = callback

    # homeassistant.helpers.event.async_call_later — the widget-refresh coalescer
    # (Phase 8). Controllable stub: records each scheduled flush so a test can
    # fire it manually and returns a cancel handle.
    helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    event_mod = sys.modules.setdefault(
        "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
    )
    if not hasattr(event_mod, "async_call_later"):
        event_mod.calls = []

        def async_call_later(hass, delay, action):
            rec = {"delay": delay, "action": action, "cancelled": False}
            event_mod.calls.append(rec)

            def _cancel() -> None:
                rec["cancelled"] = True

            return _cancel

        event_mod.async_call_later = async_call_later
    ha.helpers = helpers
    helpers.event = event_mod


_install_ha_stubs()

if "casasmart" not in sys.modules:
    _pkg = types.ModuleType("casasmart")
    _pkg.__path__ = [str(_PKG)]
    sys.modules["casasmart"] = _pkg

import aiohttp  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from casasmart.push_crypto import PushSigner  # noqa: E402
from casasmart.push_dispatcher import (  # noqa: E402
    PRIORITY_CRITICAL,
    PRIORITY_NORMAL,
    PUSH_TYPE_LOCK,
    PUSH_TYPE_SECURITY,
    PushDispatcher,
)
from const import EVENT_ALARM_TRIGGERED  # noqa: E402
from push import PushTokenStore  # noqa: E402
from storage import HubStorage  # noqa: E402


# -- fakes --------------------------------------------------------------------
class FakeState:
    def __init__(self, state: str, friendly_name: str | None = None) -> None:
        self.state = state
        self.attributes = {"friendly_name": friendly_name} if friendly_name else {}


class FakeStates:
    def __init__(self) -> None:
        self._states: dict[str, FakeState] = {}

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    def set(self, entity_id: str, state: FakeState) -> None:
        self._states[entity_id] = state


class FakeBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list] = {}

    def async_listen(self, event_type: str, handler):
        self._listeners.setdefault(event_type, []).append(handler)

        def _unsub() -> None:
            self._listeners[event_type].remove(handler)

        return _unsub

    def fire(self, event_type: str, data: dict) -> None:
        from homeassistant.core import Event

        for handler in list(self._listeners.get(event_type, [])):
            handler(Event(data))


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.states = FakeStates()
        self.created: list = []  # coroutines spawned by async_create_task

    def async_create_task(self, coro):
        self.created.append(coro)
        return coro

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeReqCtx:
    def __init__(self, response, error) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakeSession:
    """Stands in for an aiohttp ClientSession's ``post`` context manager."""

    def __init__(self, status: int = 200, payload=None, error=None) -> None:
        self.status = status
        self.payload = (
            payload if payload is not None else {"accepted": 1, "failed": 0, "errors": []}
        )
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 — mirror aiohttp
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeReqCtx(FakeResponse(self.status, self.payload), self.error)


_RELAY_URL = "https://relay.example/push"
_HUB_ID = "a1b2c3d4deadbeef"


class DispatcherTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.push_store = PushTokenStore(self.storage.table("push_tokens"))
        self.signer = PushSigner(Ed25519PrivateKey.generate())
        self.hass = FakeHass()
        self.session = FakeSession()

    def _make_dispatcher(self, clock=None) -> PushDispatcher:
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        dispatcher = PushDispatcher(
            self.hass,
            push_store=self.push_store,
            signer=self.signer,
            hub_id=_HUB_ID,
            relay_url=_RELAY_URL,
            session=self.session,
            **kwargs,
        )
        dispatcher.async_start()
        return dispatcher

    async def _drain(self) -> None:
        """Run every coroutine the dispatcher scheduled, then clear the queue."""
        pending, self.hass.created = self.hass.created, []
        if pending:
            await asyncio.gather(*pending)

    def _lock_event(self, entity_id, old, new):
        return {
            "entity_id": entity_id,
            "old_state": FakeState(old) if old is not None else None,
            "new_state": FakeState(new) if new is not None else None,
        }

    def _verify_signature(self, body: dict) -> None:
        signed = {k: body[k] for k in ("hub_id", "timestamp", "nonce", "priority", "payloads")}
        canonical = json.dumps(
            signed, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.signer.public_key_hex))
        pub.verify(base64.b64decode(body["signature"]), canonical)  # raises -> fail


class LockTransitionTests(DispatcherTestCase):
    async def test_unlock_dispatches_normal_priority(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self.hass.states.set(
            "lock.front", FakeState("unlocked", friendly_name="Front Door")
        )
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()

        self.assertEqual(len(self.session.calls), 1)
        body = self.session.calls[0]["json"]
        self.assertEqual(self.session.calls[0]["url"], _RELAY_URL)
        self.assertEqual(body["hub_id"], _HUB_ID)
        self.assertEqual(body["priority"], PRIORITY_NORMAL)
        self.assertEqual(len(body["payloads"]), 1)
        payload = body["payloads"][0]
        self.assertEqual(payload["device_token"], "fcm-1")
        self.assertEqual(payload["data"]["type"], PUSH_TYPE_LOCK)
        self.assertEqual(payload["data"]["title"], "Door unlocked")
        self.assertEqual(payload["data"]["body"], "Front Door was unlocked")
        self.assertEqual(payload["data"]["entity_id"], "lock.front")

    async def test_intermediate_state_then_settle_dispatches(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()
        # A lock that reports an intermediate "locking" before settling.
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locking", "locked")
        )
        await self._drain()
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(
            self.session.calls[0]["json"]["payloads"][0]["data"]["title"], "Door locked"
        )

    async def test_flaps_and_non_locks_are_ignored(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()
        ignored = [
            ("state_changed", self._lock_event("lock.front", "unavailable", "unlocked")),
            ("state_changed", self._lock_event("lock.front", "unknown", "locked")),
            ("state_changed", self._lock_event("lock.front", "locked", "unavailable")),
            ("state_changed", self._lock_event("lock.front", "locked", None)),
            ("state_changed", self._lock_event("lock.front", "locked", "locked")),
            ("state_changed", self._lock_event("lock.front", None, "locked")),
            ("state_changed", self._lock_event("binary_sensor.door", "locked", "unlocked")),
            ("state_changed", {"entity_id": None}),
        ]
        for event_type, data in ignored:
            self.hass.bus.fire(event_type, data)
        await self._drain()
        self.assertEqual(self.hass.created, [])
        self.assertEqual(self.session.calls, [])

    async def test_falls_back_to_entity_id_without_friendly_name(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()  # no state registered -> no friendly_name
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.garage", "unlocked", "locked")
        )
        await self._drain()
        body = self.session.calls[0]["json"]
        self.assertEqual(body["payloads"][0]["data"]["body"], "lock.garage was locked")


class AlarmTests(DispatcherTestCase):
    async def test_alarm_triggered_dispatches_critical_security(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self.hass.states.set(
            "binary_sensor.front_door",
            FakeState("on", friendly_name="Front Door"),
        )
        self._make_dispatcher()
        self.hass.bus.fire(
            EVENT_ALARM_TRIGGERED,
            {
                "kind": "triggered",
                "entity_id": "binary_sensor.front_door",
                "zone": "perimeter",
                "life_safety": False,
            },
        )
        await self._drain()
        body = self.session.calls[0]["json"]
        self.assertEqual(body["priority"], PRIORITY_CRITICAL)
        data = body["payloads"][0]["data"]
        self.assertEqual(data["type"], PUSH_TYPE_SECURITY)
        self.assertEqual(data["title"], "Security alarm")
        self.assertEqual(data["body"], "Front Door triggered the alarm")
        self.assertEqual(data["entity_id"], "binary_sensor.front_door")

    async def test_life_safety_changes_the_title(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()
        self.hass.bus.fire(
            EVENT_ALARM_TRIGGERED,
            {"kind": "life_safety", "zone": "kitchen", "life_safety": True},
        )
        await self._drain()
        data = self.session.calls[0]["json"]["payloads"][0]["data"]
        self.assertEqual(data["title"], "Life-safety alarm")
        # No entity_id available -> the optional key is omitted (never null).
        self.assertNotIn("entity_id", data)


class WireContractTests(DispatcherTestCase):
    async def test_no_tokens_means_no_send(self) -> None:
        self._make_dispatcher()  # nothing registered
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        self.assertEqual(self.session.calls, [])

    async def test_signature_verifies_over_canonical(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        self._verify_signature(self.session.calls[0]["json"])

    async def test_timestamp_is_int_not_float(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher(clock=lambda: 1700000000.987)
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        ts = self.session.calls[0]["json"]["timestamp"]
        self.assertIsInstance(ts, int)
        self.assertEqual(ts, 1700000000)

    async def test_batch_targets_every_registered_token(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self.push_store.register("dev-2", "fcm-2", "android")
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        tokens = {p["device_token"] for p in self.session.calls[0]["json"]["payloads"]}
        self.assertEqual(tokens, {"fcm-1", "fcm-2"})


class TokenCleanupTests(DispatcherTestCase):
    async def test_remove_token_action_unregisters_that_device(self) -> None:
        self.push_store.register("dev-live", "fcm-live", "ios")
        self.push_store.register("dev-dead", "fcm-dead", "ios")
        self.session = FakeSession(
            payload={
                "accepted": 1,
                "failed": 1,
                "errors": [
                    {
                        "device_token": "fcm-dead",
                        "code": "UNREGISTERED",
                        "message": "gone",
                        "action": "remove_token",
                    }
                ],
            }
        )
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        self.assertIsNone(self.push_store.get_token("dev-dead"))
        self.assertIsNotNone(self.push_store.get_token("dev-live"))

    async def test_retry_later_action_keeps_the_token(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self.session = FakeSession(
            payload={
                "accepted": 0,
                "failed": 1,
                "errors": [
                    {"device_token": "fcm-1", "code": "INTERNAL", "action": "retry_later"}
                ],
            }
        )
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        self.assertIsNotNone(self.push_store.get_token("dev-1"))


class ResilienceTests(DispatcherTestCase):
    async def test_unreachable_relay_is_swallowed(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self.session = FakeSession(error=aiohttp.ClientError("connection refused"))
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()  # must not raise
        self.assertIsNotNone(self.push_store.get_token("dev-1"))  # untouched

    async def test_non_200_response_does_not_clean_tokens(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        self.session = FakeSession(status=429, payload={"error": "rate_limited"})
        self._make_dispatcher()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        await self._drain()
        self.assertIsNotNone(self.push_store.get_token("dev-1"))

    async def test_stop_unsubscribes(self) -> None:
        self.push_store.register("dev-1", "fcm-1", "ios")
        dispatcher = self._make_dispatcher()
        dispatcher.async_stop()
        self.hass.bus.fire(
            "state_changed", self._lock_event("lock.front", "locked", "unlocked")
        )
        self.hass.bus.fire(EVENT_ALARM_TRIGGERED, {"kind": "triggered"})
        await self._drain()
        self.assertEqual(self.session.calls, [])


class TransitionMatrixTests(unittest.TestCase):
    """Direct coverage of the pure settled-transition predicate."""

    def _t(self, old, new) -> bool:
        return PushDispatcher._is_real_lock_transition(
            FakeState(old) if old is not None else None,
            FakeState(new) if new is not None else None,
        )

    def test_real_transitions(self) -> None:
        self.assertTrue(self._t("locked", "unlocked"))
        self.assertTrue(self._t("unlocked", "locked"))
        self.assertTrue(self._t("locking", "locked"))
        self.assertTrue(self._t("unlocking", "unlocked"))

    def test_non_transitions(self) -> None:
        self.assertFalse(self._t("locked", "locked"))
        self.assertFalse(self._t("unavailable", "locked"))
        self.assertFalse(self._t("unknown", "unlocked"))
        self.assertFalse(self._t("locked", "unavailable"))
        self.assertFalse(self._t("locked", "locking"))
        self.assertFalse(self._t(None, "locked"))
        self.assertFalse(self._t("locked", None))


class WidgetRefreshTests(DispatcherTestCase):
    """Coalesced silent widget-refresh push (Phase 8)."""

    def _ev(self, entity_id, old, new):
        return {
            "entity_id": entity_id,
            "old_state": FakeState(old) if old is not None else None,
            "new_state": FakeState(new) if new is not None else None,
        }

    async def test_control_edges_coalesce_into_one_silent_push(self) -> None:
        import homeassistant.helpers.event as ev

        ev.calls.clear()
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()

        # Two settled control edges within the window → ONE flush scheduled.
        self.hass.bus.fire("state_changed", self._ev("light.kitchen", "off", "on"))
        self.hass.bus.fire("state_changed", self._ev("switch.fan", "off", "on"))
        self.assertEqual(len(ev.calls), 1, "coalesced to a single flush")

        # Nothing sent until the coalescing window elapses.
        await self._drain()
        self.assertEqual(len(self.session.calls), 0)

        # Fire the timer → exactly one silent update_widgets push.
        ev.calls[0]["action"](None)
        await self._drain()
        self.assertEqual(len(self.session.calls), 1)
        body = self.session.calls[0]["json"]
        self.assertEqual(body["priority"], PRIORITY_NORMAL)
        data = body["payloads"][0]["data"]
        self.assertEqual(data["type"], "update_widgets")
        self.assertEqual(data["silent"], "1")
        self.assertNotIn("title", data)
        self._verify_signature(body)

    async def test_widget_push_goes_house_wide_not_owner_only(self) -> None:
        import homeassistant.helpers.event as ev

        ev.calls.clear()
        self.push_store.register("dev-owner", "fcm-owner", "ios")
        self.push_store.register("dev-room", "fcm-room", "android")
        self._make_dispatcher()
        self.hass.bus.fire("state_changed", self._ev("light.kitchen", "off", "on"))
        ev.calls[0]["action"](None)
        await self._drain()

        tokens = {
            p["device_token"] for p in self.session.calls[0]["json"]["payloads"]
        }
        self.assertEqual(tokens, {"fcm-owner", "fcm-room"})

    async def test_irrelevant_changes_do_not_schedule(self) -> None:
        import homeassistant.helpers.event as ev

        ev.calls.clear()
        self.push_store.register("dev-1", "fcm-1", "ios")
        self._make_dispatcher()

        # Excluded domain (sensor churns constantly), no-op change, and a flap
        # in from unavailable — none is a real settled control edge.
        self.hass.bus.fire("state_changed", self._ev("sensor.power", "100", "105"))
        self.hass.bus.fire("state_changed", self._ev("light.kitchen", "on", "on"))
        self.hass.bus.fire("state_changed", self._ev("light.kitchen", "unavailable", "on"))
        self.assertEqual(len(ev.calls), 0)
        await self._drain()
        self.assertEqual(len(self.session.calls), 0)


class WidgetRelevanceMatrixTests(unittest.TestCase):
    """Direct coverage of the widget-relevant predicate."""

    def _r(self, entity_id, old, new) -> bool:
        return PushDispatcher._is_widget_relevant_change(
            entity_id,
            FakeState(old) if old is not None else None,
            FakeState(new) if new is not None else None,
        )

    def test_relevant(self) -> None:
        self.assertTrue(self._r("light.k", "off", "on"))
        self.assertTrue(self._r("cover.garage", "closed", "open"))
        self.assertTrue(self._r("climate.ac", "off", "cool"))
        self.assertTrue(self._r("lock.front", "locked", "unlocked"))

    def test_irrelevant(self) -> None:
        self.assertFalse(self._r("sensor.power", "100", "105"))  # excluded domain
        self.assertFalse(self._r("binary_sensor.motion", "off", "on"))
        self.assertFalse(self._r("light.k", "on", "on"))  # no change
        self.assertFalse(self._r("light.k", "unavailable", "on"))  # flap in
        self.assertFalse(self._r("light.k", "on", "unknown"))  # flap out
        self.assertFalse(self._r("light.k", None, "on"))


if __name__ == "__main__":
    unittest.main()
