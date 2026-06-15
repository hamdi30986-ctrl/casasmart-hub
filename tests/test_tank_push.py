"""Unit tests for B8 Piece 4b: the tank low-water + offline push monitor.

``TankPushMonitor`` lives in ``push_dispatcher.py`` (HA glue), so — like the
dispatcher tests — light ``homeassistant.*`` stubs go into ``sys.modules`` before
the import. Everything tank-side is REAL: a ``TankEngine`` over a temp DB does
the calibration math and owns the readings; only ``hass`` and the notifier are
fakes. The monitor's two decision methods are driven directly with an injected
clock and table-stamped reading timestamps, so the schedule is deterministic
without real HA timers or wall-clock waits.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))


def _install_ha_stubs() -> None:
    """Augment (never clobber) the HA stub modules the dispatcher imports."""
    sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

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

        class Event:
            def __init__(self, data):
                self.data = data

        core.Event = Event
    if not hasattr(core, "HomeAssistant"):

        class HomeAssistant:
            pass

        core.HomeAssistant = HomeAssistant
    if not hasattr(core, "callback"):

        def callback(fn):
            return fn

        core.callback = callback


_install_ha_stubs()

if "casasmart" not in sys.modules:
    _pkg = types.ModuleType("casasmart")
    _pkg.__path__ = [str(_PKG)]
    sys.modules["casasmart"] = _pkg

from casasmart.push_dispatcher import (  # noqa: E402
    PRIORITY_NORMAL,
    TANK_OFFLINE_TIMEOUT_SECONDS,
    TankPushMonitor,
)
from const import (  # noqa: E402
    EVENT_TANK_LOW,
    EVENT_TANK_OFFLINE,
    PUSH_TYPE_TANK_LOW,
    PUSH_TYPE_TANK_OFFLINE,
)
from storage import HubStorage  # noqa: E402
from tank import TankEngine  # noqa: E402

# A fixed epoch so day bucketing is deterministic; +1 day crosses an AST day.
_T0 = 1_700_000_000
_DAY = 86400


class FakeBus:
    def __init__(self) -> None:
        self.fired: list = []

    def async_fire(self, event_type: str, data: dict) -> None:
        self.fired.append((event_type, data))


class FakeNotifier:
    """Captures what would be signed + relayed (the dispatcher's async_send)."""

    def __init__(self) -> None:
        self.sent: list = []

    async def async_send(self, data: dict, priority: str) -> None:
        self.sent.append((data, priority))


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class TankPushTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.engine = TankEngine(
            self.storage.table("tank_devices"),
            self.storage.table("tank_readings"),
        )
        self.hass = FakeHass()
        self.notifier = FakeNotifier()
        self.clock = _Clock(_T0)
        self.monitor = TankPushMonitor(
            self.hass, tanks=self.engine, notifier=self.notifier, clock=self.clock
        )

    # -- helpers ---------------------------------------------------------------

    def _calibrated(self, device_id: str, *, low_percent: int = 20) -> None:
        """A device calibrated so 4.0 V == full (1 V == 25%)."""
        self.engine.mint_device(device_id, device_id, "10.0.0.5")
        self.engine.set_calibration(
            device_id,
            calibration_voltage=2.0,
            calibration_depth=2.0,
            max_height=4.0,
            low_percent=low_percent,
        )

    def _put_reading(self, device_id: str, t: float, voltage: float) -> None:
        """Stamp a reading at an exact time so freshness is deterministic."""
        table = self.storage.table("tank_readings")
        blob = table.get(device_id) or {"entries": []}
        blob["entries"].append({"t": int(t), "v": float(voltage)})
        table[device_id] = blob

    def _fired(self, event_type: str) -> list:
        return [data for evt, data in self.hass.bus.fired if evt == event_type]


class LowWaterTests(TankPushTestCase):
    async def test_pushes_when_below_threshold(self) -> None:
        self._calibrated("dev-1", low_percent=20)
        self._put_reading("dev-1", _T0, 0.4)  # 0.4/4 -> 10% < 20
        await self.monitor.async_check_low_water()

        self.assertEqual(len(self.notifier.sent), 1)
        data, priority = self.notifier.sent[0]
        self.assertEqual(data["type"], PUSH_TYPE_TANK_LOW)
        self.assertEqual(data["title"], "Water tank low")
        self.assertIn("(10%)", data["body"])
        self.assertEqual(data["device_id"], "dev-1")
        self.assertEqual(priority, PRIORITY_NORMAL)
        # Bus event fired for installer automations.
        low_events = self._fired(EVENT_TANK_LOW)
        self.assertEqual(len(low_events), 1)
        self.assertEqual(low_events[0]["device_id"], "dev-1")
        self.assertAlmostEqual(low_events[0]["percent"], 10.0)
        self.assertEqual(low_events[0]["low_percent"], 20)

    async def test_no_push_when_above_threshold(self) -> None:
        self._calibrated("dev-1", low_percent=20)
        self._put_reading("dev-1", _T0, 2.0)  # 50% >= 20
        await self.monitor.async_check_low_water()
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._fired(EVENT_TANK_LOW), [])

    async def test_skips_uncalibrated_tank(self) -> None:
        self.engine.mint_device("dev-1", "Tank", "10.0.0.5")  # never calibrated
        self._put_reading("dev-1", _T0, 0.1)
        await self.monitor.async_check_low_water()
        self.assertEqual(self.notifier.sent, [])

    async def test_skips_tank_without_reading(self) -> None:
        self._calibrated("dev-1")  # calibrated but no reading yet
        await self.monitor.async_check_low_water()
        self.assertEqual(self.notifier.sent, [])

    async def test_skips_stale_reading(self) -> None:
        # A low reading older than the offline window must NOT raise "low" —
        # the offline watchdog owns a dead sensor, not the low sweep.
        self._calibrated("dev-1", low_percent=20)
        self._put_reading("dev-1", _T0 - TANK_OFFLINE_TIMEOUT_SECONDS - 60, 0.4)
        await self.monitor.async_check_low_water()
        self.assertEqual(self.notifier.sent, [])

    async def test_dedup_once_per_day_then_again_next_day(self) -> None:
        self._calibrated("dev-1", low_percent=20)
        self._put_reading("dev-1", _T0, 0.4)
        await self.monitor.async_check_low_water()
        await self.monitor.async_check_low_water()  # same day -> no second push
        self.assertEqual(len(self.notifier.sent), 1)

        # Next AST day, with a fresh low reading -> pushes again.
        self.clock.t = _T0 + _DAY
        self._put_reading("dev-1", _T0 + _DAY, 0.4)
        await self.monitor.async_check_low_water()
        self.assertEqual(len(self.notifier.sent), 2)

    async def test_multi_tank_only_low_one_pushes(self) -> None:
        self._calibrated("low", low_percent=20)
        self._calibrated("ok", low_percent=20)
        self._put_reading("low", _T0, 0.4)  # 10%
        self._put_reading("ok", _T0, 3.0)  # 75%
        await self.monitor.async_check_low_water()
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0][0]["device_id"], "low")


class OfflineTests(TankPushTestCase):
    async def test_pushes_after_20_minutes_silent(self) -> None:
        self._calibrated("dev-1")
        self._put_reading("dev-1", _T0, 2.0)
        self.clock.t = _T0 + TANK_OFFLINE_TIMEOUT_SECONDS + 1
        await self.monitor.async_check_offline()

        self.assertEqual(len(self.notifier.sent), 1)
        data, priority = self.notifier.sent[0]
        self.assertEqual(data["type"], PUSH_TYPE_TANK_OFFLINE)
        self.assertEqual(data["title"], "Water tank offline")
        self.assertIn("20+ minutes", data["body"])
        self.assertEqual(priority, PRIORITY_NORMAL)
        offline_events = self._fired(EVENT_TANK_OFFLINE)
        self.assertEqual(len(offline_events), 1)
        self.assertEqual(offline_events[0]["device_id"], "dev-1")

    async def test_no_push_when_recently_reporting(self) -> None:
        self._calibrated("dev-1")
        self._put_reading("dev-1", _T0, 2.0)
        self.clock.t = _T0 + 60  # one minute later — fresh
        await self.monitor.async_check_offline()
        self.assertEqual(self.notifier.sent, [])

    async def test_skips_never_reported_device(self) -> None:
        # Provisioned but no reading yet = pending, not offline.
        self._calibrated("dev-1")
        self.clock.t = _T0 + 10 * _DAY
        await self.monitor.async_check_offline()
        self.assertEqual(self.notifier.sent, [])

    async def test_offline_fires_even_when_uncalibrated(self) -> None:
        # Offline is about silence, not calibration — a reporting-then-dead
        # uncalibrated tank still warns.
        self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        self._put_reading("dev-1", _T0, 1.0)
        self.clock.t = _T0 + TANK_OFFLINE_TIMEOUT_SECONDS + 1
        await self.monitor.async_check_offline()
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0][0]["type"], PUSH_TYPE_TANK_OFFLINE)

    async def test_dedup_once_per_day_then_again_next_day(self) -> None:
        self._calibrated("dev-1")
        self._put_reading("dev-1", _T0, 2.0)
        self.clock.t = _T0 + TANK_OFFLINE_TIMEOUT_SECONDS + 1
        await self.monitor.async_check_offline()
        await self.monitor.async_check_offline()  # still same day -> one push
        self.assertEqual(len(self.notifier.sent), 1)

        self.clock.t = _T0 + _DAY + TANK_OFFLINE_TIMEOUT_SECONDS + 1
        await self.monitor.async_check_offline()  # next day -> warns again
        self.assertEqual(len(self.notifier.sent), 2)


class ChannelIndependenceTests(TankPushTestCase):
    async def test_low_and_offline_dedup_are_independent(self) -> None:
        # A low push must not suppress that day's offline push (or vice-versa).
        self._calibrated("dev-1", low_percent=20)
        self._put_reading("dev-1", _T0, 0.4)
        await self.monitor.async_check_low_water()  # 1 low push
        self.clock.t = _T0 + TANK_OFFLINE_TIMEOUT_SECONDS + 1
        await self.monitor.async_check_offline()  # 1 offline push, same day
        types_sent = sorted(data["type"] for data, _ in self.notifier.sent)
        self.assertEqual(
            types_sent, sorted([PUSH_TYPE_TANK_LOW, PUSH_TYPE_TANK_OFFLINE])
        )


class NoDeviceTests(TankPushTestCase):
    async def test_no_devices_is_a_noop(self) -> None:
        await self.monitor.async_check_low_water()
        await self.monitor.async_check_offline()
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.hass.bus.fired, [])


if __name__ == "__main__":
    unittest.main()
