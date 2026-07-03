"""Unit tests for the hub-native athan scheduler.

Pure glue like the audio adapter, so we inject light ``homeassistant.*`` stubs
into ``sys.modules`` before importing it — no real Home Assistant needed. The
prayer-time compute is delegated to ``prayer-times-calculator-offline``; the
scheduler logic (config resolution with HA-config fallback, arming past/future
prayers, the fire payload) is exercised with that compute patched to fixed
times, so those tests run anywhere. One test hits the real library when it's
installed (the container/CI) and skips otherwise.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import datetime
import sys
import types
import unittest
from datetime import timedelta, timezone
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
    event.async_track_point_in_time = lambda hass, action, when: (lambda: None)
    event.async_track_time_change = lambda hass, action, **kw: (lambda: None)
    dt.get_time_zone = lambda name: ZoneInfo(name)
    dt.utcnow = lambda: datetime.datetime.now(timezone.utc)

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


def _future_times():
    """Fixed compute: Fajr already past (skip), the rest ahead (arm)."""
    now = datetime.datetime.now(timezone.utc)

    def _compute(lat, lon, method, school, date_str):
        return {
            "Fajr": now - timedelta(hours=3),
            "Dhuhr": now + timedelta(hours=1),
            "Asr": now + timedelta(hours=3),
            "Maghrib": now + timedelta(hours=5),
            "Isha": now + timedelta(hours=6),
        }

    return _compute


class TestLibraryCompute(unittest.TestCase):
    """Real library — skipped where prayer-times-calculator-offline isn't installed."""

    def setUp(self):
        try:
            import prayer_times_calculator_offline  # noqa: F401
        except Exception:
            self.skipTest("prayer-times-calculator-offline not installed")

    def test_jeddah_times_sane(self):
        out = A.compute_prayer_times_utc(21.7731, 39.0976, "makkah", "shafi", "2026-07-03")
        self.assertIsNotNone(out)
        self.assertEqual(set(out), set(A.PRAYER_NAMES))
        for dt in out.values():
            self.assertIsNotNone(dt.tzinfo)  # aware UTC
        # Jeddah Fajr ~ 01:1x UTC (04:1x local); Dhuhr ~ 09:2x UTC.
        self.assertEqual(out["Fajr"].astimezone(timezone.utc).hour, 1)
        self.assertEqual(out["Dhuhr"].astimezone(timezone.utc).hour, 9)

    def test_egyptian_alias_maps_to_egypt(self):
        # "egyptian" isn't a library key ("egypt" is) — must not crash/return None.
        out = A.compute_prayer_times_utc(30.0, 31.2, "egyptian", "shafi", "2026-07-03")
        self.assertIsNotNone(out)

    def test_hanafi_asr_later_than_shafi(self):
        shafi = A.compute_prayer_times_utc(21.7731, 39.0976, "makkah", "shafi", "2026-07-03")
        hanafi = A.compute_prayer_times_utc(21.7731, 39.0976, "makkah", "hanafi", "2026-07-03")
        self.assertGreater(hanafi["Asr"], shafi["Asr"])  # Hanafi Asr is later

    def test_unknown_method_falls_back(self):
        out = A.compute_prayer_times_utc(21.7731, 39.0976, "bogus", "shafi", "2026-07-03")
        self.assertIsNotNone(out)  # falls back to makkah, still computes


class TestConfigResolution(unittest.TestCase):
    def test_disabled_returns_none(self):
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": False}), _Adapter())
        self.assertIsNone(s._resolve_config())

    def test_empty_returns_none(self):
        s = A.AthanScheduler(_Hass(), _Engine({}), _Adapter())
        self.assertIsNone(s._resolve_config())

    def test_fallback_to_ha_config(self):
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": True, "method": "makkah"}), _Adapter())
        self.assertEqual(
            s._resolve_config(),
            (21.7731, 39.0976, "Asia/Riyadh", "makkah", "shafi"),
        )

    def test_config_overrides_including_school(self):
        s = A.AthanScheduler(
            _Hass(),
            _Engine({"enabled": True, "lat": 33.5, "lon": 36.3,
                     "timezone": "Europe/Istanbul", "method": "turkey",
                     "school": "hanafi"}),
            _Adapter(),
        )
        self.assertEqual(
            s._resolve_config(),
            (33.5, 36.3, "Europe/Istanbul", "turkey", "hanafi"),
        )


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
        armed = []
        A.async_track_point_in_time = lambda hass, action, when: (armed.append(when) or (lambda: None))
        A.compute_prayer_times_utc = _future_times()
        s = A.AthanScheduler(
            _Hass(),
            _Engine({"enabled": True, "lat": 21.7731, "lon": 39.0976,
                     "timezone": "Asia/Riyadh", "method": "makkah"}),
            _Adapter(),
        )
        s.reschedule()
        # Fajr was 3h past → skipped; the other four are ahead → armed.
        self.assertEqual(len(armed), 4)
        now = datetime.datetime.now(timezone.utc)
        for when in armed:
            self.assertGreater((when - now).total_seconds(), -A._GRACE_SEC)

    def test_reschedule_disabled_arms_nothing(self):
        armed = []
        A.async_track_point_in_time = lambda hass, action, when: (armed.append(when) or (lambda: None))
        A.compute_prayer_times_utc = _future_times()
        A.AthanScheduler(_Hass(), _Engine({"enabled": False}), _Adapter()).reschedule()
        self.assertEqual(armed, [])


if __name__ == "__main__":
    unittest.main()
