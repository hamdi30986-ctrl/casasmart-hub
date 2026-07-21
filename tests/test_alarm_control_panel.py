"""Unit tests for B13 piece 3: the alarm_control_panel HA entity.

Like the adapter, the panel imports ``homeassistant.*`` at module top, so HA
symbols are stubbed into ``sys.modules`` before import. The engine underneath
is the REAL ``AlarmEngine`` over temp storage with a hand-cranked clock — the
panel is exercised against the actual engine contract, not a mock of it.

The stubs come from the SHARED package (``tests/hastubs``) that every suite
installs, so this runs cleanly under ``python3 -m unittest discover -s tests``
alongside test_alarm_adapter — in any order.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))


# Shared homeassistant stub package (tests/hastubs): the panel's
# alarm_control_panel classes (Entity/EntityFeature/State) live there now.
# Every test patches ``acp_mod.async_call_later`` itself, so the shared
# stub's recording default never runs here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

import casasmart.alarm_control_panel as acp_mod  # noqa: E402
from casasmart.alarm_control_panel import CasaSmartAlarmPanel  # noqa: E402
from storage import HubStorage  # noqa: E402
from const import EVENT_ALARM_CHANGED  # noqa: E402
from alarm import (  # noqa: E402
    MODE_AWAY,
    MODE_DISARMED,
    MODE_HOME,
    MODE_NIGHT,
    ZONE_ENTRY,
    ZONE_PERIMETER,
    AlarmEngine,
)

from homeassistant.components.alarm_control_panel import (  # noqa: E402
    AlarmControlPanelState as State,
)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeBus:
    """Auto-dispatches to listeners synchronously (mirrors a fired event
    reaching the panel's own EVENT_ALARM_CHANGED handler on the loop)."""

    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.fired: list[tuple[str, dict]] = []

    def async_listen(self, event_type, cb):
        self.listeners.setdefault(event_type, []).append(cb)
        return lambda: self.listeners[event_type].remove(cb)

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))
        for cb in list(self.listeners.get(event_type, [])):
            cb(types.SimpleNamespace(data=data or {}))

    def count(self, event_type) -> int:
        return sum(1 for ev, _ in self.fired if ev == event_type)


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeTimer:
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, object]] = []
        self.cancels = 0

    def __call__(self, hass, delay, action):
        self.scheduled.append((delay, action))
        return self._cancel

    def _cancel(self):
        self.cancels += 1

    @property
    def last_delay(self):
        return self.scheduled[-1][0] if self.scheduled else None

    @property
    def last_action(self):
        return self.scheduled[-1][1] if self.scheduled else None


class AlarmPanelTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)

        self.clock = _Clock()
        self.engine = AlarmEngine(
            self.storage.table("alarm_state"),
            self.storage.table("alarm_zones"),
            self.storage.table("alarm_history"),
            self.storage.table("alarm_settings"),
            clock=self.clock,
        )
        self.engine.warm_up()
        self.engine.set_zone("binary_sensor.front_door", ZONE_ENTRY, "Front Door")
        self.engine.set_zone("binary_sensor.window", ZONE_PERIMETER, "Window")

        # Panel reads wall-clock for the ARMING derivation; pin it to the same
        # clock so arming-window assertions are exact.
        self._time_patch = mock.patch.object(acp_mod.time, "time", self.clock)
        self._time_patch.start()
        self.addCleanup(self._time_patch.stop)

        self.timer = _FakeTimer()
        self._timer_patch = mock.patch.object(
            acp_mod, "async_call_later", self.timer
        )
        self._timer_patch.start()
        self.addCleanup(self._timer_patch.stop)

        self.hass = _FakeHass()
        self.panel = CasaSmartAlarmPanel(self.hass, "entry-123", self.engine)
        await self.panel.async_added_to_hass()

    # -- state mapping ---------------------------------------------------------

    async def test_disarmed_by_default(self):
        self.assertEqual(self.panel.alarm_state, State.DISARMED)

    async def test_each_mode_maps_to_its_state(self):
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.assertEqual(self.panel.alarm_state, State.ARMED_AWAY)
        self.engine.arm(MODE_HOME, exit_delay=0)
        self.assertEqual(self.panel.alarm_state, State.ARMED_HOME)
        self.engine.arm(MODE_NIGHT, exit_delay=0)
        self.assertEqual(self.panel.alarm_state, State.ARMED_NIGHT)

    async def test_arming_during_exit_grace_then_armed(self):
        self.engine.arm(MODE_AWAY, exit_delay=60)
        self.assertEqual(self.panel.alarm_state, State.ARMING)
        self.clock.advance(61)
        self.assertEqual(self.panel.alarm_state, State.ARMED_AWAY)

    async def test_pending_and_triggered(self):
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertEqual(self.panel.alarm_state, State.PENDING)
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.window", True)
        self.assertEqual(self.panel.alarm_state, State.TRIGGERED)

    # -- commands route into the one engine + announce -------------------------

    async def test_arm_away_command_drives_engine_and_fires_event(self):
        await self.panel.async_alarm_arm_away()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_AWAY)
        self.assertEqual(self.hass.bus.count(EVENT_ALARM_CHANGED), 1)
        # actor recorded as homeassistant, not an app user
        self.assertEqual(self.engine.history()[0]["actor"], "homeassistant")

    async def test_disarm_command(self):
        self.engine.arm(MODE_AWAY, exit_delay=0)
        await self.panel.async_alarm_disarm()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_DISARMED)

    # -- app-driven change reflects on the panel -------------------------------

    async def test_external_alarm_changed_repaints_panel(self):
        before = self.panel.write_count
        # Simulate the REST API firing the event after an app-driven arm.
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.hass.bus.async_fire(EVENT_ALARM_CHANGED, {})
        self.assertGreater(self.panel.write_count, before)
        self.assertEqual(self.panel.alarm_state, State.ARMED_AWAY)

    # -- arming refresh timer --------------------------------------------------

    async def test_arming_schedules_one_refresh_at_deadline(self):
        self.engine.arm(MODE_AWAY, exit_delay=45)
        self.hass.bus.async_fire(EVENT_ALARM_CHANGED, {})
        self.assertEqual(self.timer.last_delay, 45)
        # firing the deadline action repaints to the armed state
        self.clock.advance(46)
        self.timer.last_action(None)
        self.assertEqual(self.panel.alarm_state, State.ARMED_AWAY)

    async def test_no_arming_timer_when_no_exit_delay(self):
        self.engine.arm(MODE_AWAY, exit_delay=0)
        scheduled_before = len(self.timer.scheduled)
        self.hass.bus.async_fire(EVENT_ALARM_CHANGED, {})
        # mode is armed but grace already spent -> no new timer
        self.assertEqual(len(self.timer.scheduled), scheduled_before)

    async def test_removal_unsubscribes_and_cancels(self):
        self.engine.arm(MODE_AWAY, exit_delay=60)
        self.hass.bus.async_fire(EVENT_ALARM_CHANGED, {})
        cancels_before = self.timer.cancels
        await self.panel.async_will_remove_from_hass()
        self.assertGreater(self.timer.cancels, cancels_before)
        self.assertEqual(self.hass.bus.listeners.get(EVENT_ALARM_CHANGED), [])


if __name__ == "__main__":
    unittest.main()
