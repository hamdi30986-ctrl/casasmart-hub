"""Unit tests for the hub-native athan scheduler.

Pure glue like the audio adapter, so we inject light ``homeassistant.*`` stubs
into ``sys.modules`` before importing it — no real Home Assistant needed. The
prayer-time math, the DST-aware offset, the config resolution (with HA-config
fallback), and the fire payload are all exercised without a running loop; the
HA scheduling helpers are stubbed to just record what was armed.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import datetime
import sys
import types
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))


def _install_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")

    class HomeAssistant:  # never instantiated — _FakeHass duck-types it
        pass

    def callback(fn):
        return fn

    core.HomeAssistant = HomeAssistant
    core.callback = callback
    # Overridden per-test; defaults are inert no-ops returning an unsub.
    event.async_track_point_in_time = lambda hass, action, when: (lambda: None)
    event.async_track_time_change = lambda hass, action, **kw: (lambda: None)
    dt.get_time_zone = lambda name: ZoneInfo(name)

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.event"] = event
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt


_install_ha_stubs()

if "casasmart" not in sys.modules:
    _pkg = types.ModuleType("casasmart")
    _pkg.__path__ = [str(_PKG)]
    sys.modules["casasmart"] = _pkg

import casasmart.athan_scheduler as A  # noqa: E402


class _Cfg:
    latitude = 21.7731
    longitude = 39.0976
    time_zone = "Asia/Riyadh"


class _Hass:
    config = _Cfg()


class _Engine:
    def __init__(self, athan):
        self._athan = athan

    def get_athan(self):
        return self._athan

    def build_play(self, *, file, priority):
        return ("speakers/broadcast", {"cmd": "play", "file": file, "priority": priority})


class _Adapter:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload))


def _hm(hours):
    hours %= 24
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h:02d}:{m:02d}"


class TestPrayerMath(unittest.TestCase):
    def test_matches_reference_daemon_jeddah(self):
        # The verified daemon's output for Jeddah (UTC+3, makkah), floored.
        t = A.prayer_times_local(datetime.date(2026, 7, 3), 21.7731, 39.0976, 3, "makkah")
        got = {k: _hm(v) for k, v in t.items()}
        self.assertEqual(
            got,
            {"Fajr": "04:16", "Dhuhr": "12:27", "Asr": "15:45",
             "Maghrib": "19:10", "Isha": "20:40"},
        )

    def test_method_angles(self):
        self.assertEqual(A._method_params("mwl")["fajr"], 18.0)
        self.assertEqual(A._method_params("egyptian")["fajr"], 19.5)
        self.assertEqual(A._method_params("isna")["isha"], 15.0)
        # Umm-al-Qura uses a fixed 90-min isha, not an angle.
        self.assertIsNone(A._method_params("makkah")["isha"])
        self.assertEqual(A._method_params("makkah")["isha_min"], 90)
        # Unknown falls back to makkah.
        self.assertEqual(A._method_params("bogus"), A._method_params("makkah"))


class TestDstOffset(unittest.TestCase):
    def test_riyadh_no_dst(self):
        tz = ZoneInfo("Asia/Riyadh")
        self.assertEqual(A._offset_hours(tz, datetime.date(2026, 1, 1)), 3.0)
        self.assertEqual(A._offset_hours(tz, datetime.date(2026, 7, 1)), 3.0)

    def test_new_york_dst_aware(self):
        tz = ZoneInfo("America/New_York")
        self.assertEqual(A._offset_hours(tz, datetime.date(2026, 1, 1)), -5.0)  # EST
        self.assertEqual(A._offset_hours(tz, datetime.date(2026, 7, 1)), -4.0)  # EDT


class TestConfigResolution(unittest.TestCase):
    def test_disabled_returns_none(self):
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": False}), _Adapter())
        self.assertIsNone(s._resolve_config())

    def test_empty_returns_none(self):
        s = A.AthanScheduler(_Hass(), _Engine({}), _Adapter())
        self.assertIsNone(s._resolve_config())

    def test_fallback_to_ha_config(self):
        # enabled, but no lat/lon/timezone → inherit the hub's HA location.
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": True, "method": "makkah"}), _Adapter())
        self.assertEqual(s._resolve_config(), (21.7731, 39.0976, "Asia/Riyadh", "makkah"))

    def test_config_overrides_ha(self):
        s = A.AthanScheduler(
            _Hass(),
            _Engine({"enabled": True, "lat": 33.5, "lon": 36.3,
                     "timezone": "Europe/Istanbul", "method": "mwl"}),
            _Adapter(),
        )
        self.assertEqual(s._resolve_config(), (33.5, 36.3, "Europe/Istanbul", "mwl"))


class TestFireAndArming(unittest.TestCase):
    def test_fire_builds_broadcast_play(self):
        adapter = _Adapter()
        s = A.AthanScheduler(
            _Hass(),
            _Engine({"enabled": True, "lat": 21.7731, "lon": 39.0976,
                     "timezone": "Asia/Riyadh", "method": "makkah"}),
            adapter,
        )
        s._fire_athan("Fajr")
        self.assertEqual(len(adapter.published), 1)
        topic, payload = adapter.published[0]
        self.assertEqual(topic, "speakers/broadcast")
        self.assertEqual(payload["file"], "/var/lib/speaker/athans/fajr.mp3")
        self.assertEqual(payload["priority"], "athan")

    def test_fire_skips_when_disabled(self):
        adapter = _Adapter()
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": False}), adapter)
        s._fire_athan("Fajr")
        self.assertEqual(adapter.published, [])

    def test_reschedule_arms_only_future_prayers(self):
        # Drive a deterministic "now" by arming against a fixed set: patch the
        # tracker to record fire times, then assert every armed time is in the
        # future relative to real now and there are at most 5.
        armed = []
        A.async_track_point_in_time = lambda hass, action, when: (armed.append(when) or (lambda: None))
        s = A.AthanScheduler(
            _Hass(),
            _Engine({"enabled": True, "lat": 21.7731, "lon": 39.0976,
                     "timezone": "Asia/Riyadh", "method": "makkah"}),
            _Adapter(),
        )
        s.reschedule()
        self.assertLessEqual(len(armed), 5)
        now = datetime.datetime.now(ZoneInfo("Asia/Riyadh"))
        for when in armed:
            # never arm something already well past (grace = 120s)
            self.assertGreater((when - now).total_seconds(), -A._GRACE_SEC)

    def test_reschedule_disabled_arms_nothing(self):
        armed = []
        A.async_track_point_in_time = lambda hass, action, when: (armed.append(when) or (lambda: None))
        A.AthanScheduler(_Hass(), _Engine({"enabled": False}), _Adapter()).reschedule()
        self.assertEqual(armed, [])


if __name__ == "__main__":
    unittest.main()
