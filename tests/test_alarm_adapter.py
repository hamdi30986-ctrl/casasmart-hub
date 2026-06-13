"""Unit tests for B13 piece 2: the hub-side alarm adapter (HA glue).

The adapter is pure Home Assistant glue, so unlike the engine it imports
``homeassistant.*`` at module top. HA isn't installed in the test env, so we
inject light stubs into ``sys.modules`` BEFORE importing the adapter. The
engine itself is the REAL ``AlarmEngine`` over temp storage with a hand-cranked
clock — this exercises the adapter against the actual engine contract rather
than a mock that could drift from it.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
# Bare names (alarm/const/storage) resolve via the package dir, like the other
# suites. The adapter uses package-relative imports (``from .alarm import``),
# so it must be loaded through a real ``casasmart`` package — see below.
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))


# -- homeassistant stubs (installed before importing the adapter) -------------
def _install_ha_stubs() -> None:
    """Minimal stand-ins for the exact HA symbols the adapter imports."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")

    const = types.ModuleType("homeassistant.const")
    const.STATE_ON = "on"
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"

    core = types.ModuleType("homeassistant.core")

    class Event:  # only ``.data`` is touched by the adapter
        def __init__(self, data):
            self.data = data

    class HomeAssistant:  # never instantiated here — FakeHass duck-types it
        pass

    def callback(fn):  # the @callback decorator is a no-op marker in HA
        return fn

    core.Event = Event
    core.HomeAssistant = HomeAssistant
    core.callback = callback

    helpers = types.ModuleType("homeassistant.helpers")
    helpers_event = types.ModuleType("homeassistant.helpers.event")

    def async_call_later(hass, delay, action):  # patched per-test
        raise AssertionError("async_call_later must be patched in tests")

    helpers_event.async_call_later = async_call_later

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.event"] = helpers_event


_install_ha_stubs()

# Register a stub ``casasmart`` package so the adapter's package-relative
# imports resolve WITHOUT running the real __init__.py (it pulls in the full
# homeassistant runtime). __path__ points at the source dir so submodules load.
if "casasmart" not in sys.modules:
    _pkg = types.ModuleType("casasmart")
    _pkg.__path__ = [str(_PKG)]
    sys.modules["casasmart"] = _pkg

import casasmart.alarm_adapter as alarm_adapter  # noqa: E402
from casasmart.alarm_adapter import AlarmAdapter  # noqa: E402
from storage import HubStorage  # noqa: E402
from const import EVENT_ALARM_CHANGED, EVENT_ALARM_TRIGGERED  # noqa: E402
from alarm import (  # noqa: E402
    DEFAULT_ENTRY_DELAY_SECONDS,
    MODE_AWAY,
    MODE_DISARMED,
    MODE_NIGHT,
    MODE_PENDING,
    MODE_TRIGGERED,
    ZONE_ENTRY,
    ZONE_INTERIOR,
    ZONE_LIFE_SAFETY,
    ZONE_PERIMETER,
    AlarmEngine,
)


# -- fakes --------------------------------------------------------------------
class _Clock:
    """Hand-cranked clock shared by the engine AND the adapter's time.time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeState:
    """Duck-typed stand-in for homeassistant.core.State (only ``.state``)."""

    def __init__(self, state: str) -> None:
        self.state = state


class _FakeBus:
    """Records listeners + fired events; does NOT auto-dispatch.

    Not auto-dispatching is deliberate: in HA ``async_fire`` hits listeners on
    the loop. Keeping it inert lets each test drive ``EVENT_ALARM_CHANGED``
    re-syncs explicitly via ``_emit`` and assert on exactly what was fired.
    """

    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.fired: list[tuple[str, dict]] = []

    def async_listen(self, event_type, callback):
        self.listeners.setdefault(event_type, []).append(callback)

        def _unsub():
            self.listeners[event_type].remove(callback)

        return _unsub


    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))

    def kinds_fired(self, event_type) -> int:
        return sum(1 for ev, _ in self.fired if ev == event_type)


class _FakeHass:
    """Duck-typed HomeAssistant: collects scheduled coros, runs jobs inline."""

    def __init__(self) -> None:
        self.bus = _FakeBus()
        self._pending_coros: list = []

    def async_create_task(self, coro):
        self._pending_coros.append(coro)
        return coro

    async def async_add_executor_job(self, func, *args):
        # The real hub hops to a thread; for the engine (sync) calling inline
        # is behaviourally identical.
        return func(*args)


class _FakeTimer:
    """Stand-in for async_call_later: records the scheduled action + delay."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, object]] = []
        self.cancels = 0

    def __call__(self, hass, delay, action):
        self.scheduled.append((delay, action))

        def _cancel():
            self.cancels += 1

        return _cancel

    @property
    def last_delay(self):
        return self.scheduled[-1][0] if self.scheduled else None

    @property
    def last_action(self):
        return self.scheduled[-1][1] if self.scheduled else None


class AlarmAdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)

        self.clock = _Clock()
        self.alerts: list[dict] = []
        self.engine = self._make_engine()
        self._seed_zones()

        # Adapter reads wall-clock via time.time(); pin it to the same clock so
        # entry-delay -> scheduled-delay assertions are exact.
        self._time_patch = mock.patch.object(alarm_adapter.time, "time", self.clock)
        self._time_patch.start()
        self.addCleanup(self._time_patch.stop)

        self.timer = _FakeTimer()
        self._timer_patch = mock.patch.object(
            alarm_adapter, "async_call_later", self.timer
        )
        self._timer_patch.start()
        self.addCleanup(self._timer_patch.stop)

        self.hass = _FakeHass()
        self.adapter = AlarmAdapter(self.hass, self.engine)

    def tearDown(self):
        # Close any coroutine a test scheduled but intentionally didn't drain
        # (e.g. the "task was scheduled" assertion) so no "never awaited" warns.
        for coro in self.hass._pending_coros:
            coro.close()
        self.hass._pending_coros.clear()

    def _make_engine(self) -> AlarmEngine:
        engine = AlarmEngine(
            self.storage.table("alarm_state"),
            self.storage.table("alarm_zones"),
            self.storage.table("alarm_history"),
            alert_sink=self.alerts.append,
            clock=self.clock,
        )
        engine.warm_up()
        return engine

    def _seed_zones(self):
        self.engine.set_zone("binary_sensor.front_door", ZONE_ENTRY, "Front Door")
        self.engine.set_zone("binary_sensor.window", ZONE_PERIMETER, "Living Window")
        self.engine.set_zone("binary_sensor.hall_motion", ZONE_INTERIOR, "Hall Motion")
        self.engine.set_zone("binary_sensor.smoke", ZONE_LIFE_SAFETY, "Kitchen Smoke")

    # -- helpers ---------------------------------------------------------------

    def _emit(self, event_type: str, data: dict) -> None:
        """Dispatch through the adapter's REAL registered listeners."""
        from homeassistant.core import Event  # the stub

        for cb in list(self.hass.bus.listeners.get(event_type, [])):
            cb(Event(data))

    async def _drain(self) -> None:
        """Run every coroutine the adapter scheduled via async_create_task."""
        while self.hass._pending_coros:
            coro = self.hass._pending_coros.pop(0)
            await coro

    def _state_changed(self, entity_id: str, state):
        new_state = None if state is None else _FakeState(state)
        self._emit("state_changed", {"entity_id": entity_id, "new_state": new_state})

    # -- lifecycle -------------------------------------------------------------

    def test_start_subscribes_state_and_alarm_listeners(self):
        self.adapter.async_start()
        self.assertEqual(len(self.hass.bus.listeners.get("state_changed", [])), 1)
        self.assertEqual(len(self.hass.bus.listeners.get(EVENT_ALARM_CHANGED, [])), 1)

    def test_stop_unsubscribes_and_is_idempotent(self):
        self.adapter.async_start()
        self.adapter.async_stop()
        self.assertEqual(self.hass.bus.listeners.get("state_changed", []), [])
        self.assertEqual(self.hass.bus.listeners.get(EVENT_ALARM_CHANGED, []), [])
        # Second stop must not raise (e.g. on a double unload).
        self.adapter.async_stop()

    def test_start_with_restored_triggered_schedules_no_timer(self):
        # A hub that died mid-countdown warms up fails-secure to TRIGGERED, so
        # there is no pending deadline and async_start must arm no timer.
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.front_door", True)
        revived = self._make_engine()
        self.assertEqual(revived.snapshot()["mode"], MODE_TRIGGERED)
        adapter = AlarmAdapter(self.hass, revived)
        adapter.async_start()
        self.assertEqual(self.timer.scheduled, [])

    # -- hot path guard --------------------------------------------------------

    def test_unmapped_edge_creates_no_task(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.ghost", "on")
        self.assertEqual(self.hass._pending_coros, [])

    def test_edge_with_no_entity_id_is_ignored(self):
        self.adapter.async_start()
        self._emit("state_changed", {"new_state": _FakeState("on")})
        self.assertEqual(self.hass._pending_coros, [])

    def test_mapped_edge_schedules_one_task(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.window", "on")
        self.assertEqual(len(self.hass._pending_coros), 1)

    # -- sensor evaluation -> engine -------------------------------------------

    async def test_active_perimeter_edge_triggers_and_sounds_siren(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.window", "on")
        await self._drain()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_CHANGED), 1)
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_TRIGGERED), 1)

    async def test_clear_edge_is_a_silent_noop(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.window", "off")
        await self._drain()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_AWAY)
        self.assertEqual(self.hass.bus.fired, [])

    async def test_offline_mapped_sensor_is_tamper_not_siren(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.hall_motion", "unavailable")
        await self._drain()
        # Tamper alerts but does NOT escalate to a full alarm or sound a siren.
        self.assertEqual(self.engine.snapshot()["mode"], MODE_AWAY)
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_CHANGED), 1)
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_TRIGGERED), 0)

    async def test_unknown_state_is_treated_as_offline(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.hall_motion", "unknown")
        await self._drain()
        self.assertEqual(self.engine.history()[0]["kind"], "tamper")

    async def test_vanished_sensor_none_state_is_offline_tamper(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.hall_motion", None)
        await self._drain()
        self.assertEqual(self.engine.history()[0]["kind"], "tamper")

    async def test_life_safety_sounds_siren_even_while_disarmed(self):
        self.adapter.async_start()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_DISARMED)
        self._state_changed("binary_sensor.smoke", "on")
        await self._drain()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_TRIGGERED), 1)

    async def test_engine_exception_is_swallowed_no_bus_fire(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        with mock.patch.object(
            self.engine, "process_sensor", side_effect=RuntimeError("boom")
        ):
            self._state_changed("binary_sensor.window", "on")
            # A failing sensor edge must never propagate out of the adapter —
            # it is caught and logged, not raised.
            with self.assertLogs("casasmart.alarm_adapter", level="ERROR") as logs:
                await self._drain()
        self.assertTrue(any("evaluation failed" in m.lower() for m in logs.output))
        self.assertEqual(self.hass.bus.fired, [])

    # -- entry-delay timer -----------------------------------------------------

    async def test_entry_delay_schedules_single_timer_with_exact_delay(self):
        self.adapter.async_start()
        self.engine.arm(MODE_NIGHT, exit_delay=0, entry_delay=45)
        self._state_changed("binary_sensor.front_door", "on")
        await self._drain()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_PENDING)
        # Entering pending fires EVENT_ALARM_CHANGED; HA loops it back to the
        # adapter, which is what actually arms the countdown timer.
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_CHANGED), 1)
        self._emit(EVENT_ALARM_CHANGED, {"mode": MODE_PENDING})
        self.assertEqual(len(self.timer.scheduled), 1)
        self.assertEqual(self.timer.last_delay, 45)

    async def test_timer_expiry_promotes_pending_to_triggered(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.front_door", "on")
        await self._drain()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_PENDING)
        self._emit(EVENT_ALARM_CHANGED, {"mode": MODE_PENDING})  # HA loopback arms timer
        # Fire the scheduled action as HA would when the delay lapses.
        self.clock.advance(DEFAULT_ENTRY_DELAY_SECONDS + 1)
        self.timer.last_action(None)
        await self._drain()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(self.hass.bus.kinds_fired(EVENT_ALARM_TRIGGERED), 1)

    async def test_alarm_changed_resync_cancels_timer_on_disarm(self):
        self.adapter.async_start()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self._state_changed("binary_sensor.front_door", "on")
        await self._drain()
        self._emit(EVENT_ALARM_CHANGED, {"mode": MODE_PENDING})  # HA loopback arms timer
        self.assertEqual(len(self.timer.scheduled), 1)
        # App-driven disarm: engine clears, then EVENT_ALARM_CHANGED fires and
        # the adapter must drop the live countdown.
        self.engine.disarm()
        self._emit(EVENT_ALARM_CHANGED, {"mode": MODE_DISARMED})
        self.assertEqual(self.timer.cancels, 1)

    def test_sync_replaces_existing_timer(self):
        self.adapter.async_start()
        # Two consecutive re-syncs with a live pending deadline: the first
        # timer must be cancelled before the second is armed (no leak).
        self.engine.arm(MODE_AWAY, exit_delay=0, entry_delay=30)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self._emit(EVENT_ALARM_CHANGED, {"mode": MODE_PENDING})
        self._emit(EVENT_ALARM_CHANGED, {"mode": MODE_PENDING})
        self.assertEqual(self.timer.cancels, 1)
        self.assertEqual(len(self.timer.scheduled), 2)


if __name__ == "__main__":
    unittest.main()
