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
import unittest
from datetime import timedelta, timezone
from pathlib import Path

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))


# Shared homeassistant stub package (tests/hastubs): provides the trackers the
# scheduler imports (inert no-op unsubs — tests that observe arming patch
# ``A.async_track_point_in_time`` directly) plus ``util.dt``.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

import casasmart.athan_scheduler as A  # noqa: E402


class _Cfg:
    latitude = 24.7136
    longitude = 46.6753
    time_zone = "Asia/Riyadh"


class _Hass:
    config = _Cfg()


class _Engine:
    def __init__(self, athan, speakers=None):
        self._athan = athan
        self._enrolled = list(speakers or [])

    def get_athan(self):
        return self._athan

    def speakers(self):
        return list(self._enrolled)

    def build_play(self, *, file, priority, mac=None):
        topic = "speakers/broadcast" if mac is None else f"speakers/{mac}/command"
        return (topic, {"cmd": "play", "file": file, "priority": priority})


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


class _RestoresModuleGlobals:
    """Snapshot the athan module globals that tests monkeypatch (the compute
    function + the two HA trackers) and restore them after each test, so a mock
    never leaks into another class — notably the real-library TestLibraryCompute.
    """

    def setUp(self):
        super().setUp()
        self._saved_globals = (
            A.compute_prayer_times_utc,
            A.async_track_point_in_time,
            A.async_track_time_change,
        )
        # Default both HA trackers to inert fakes. These used to fall through
        # to the shared stub's no-op defaults, which meant any test that didn't
        # install its own tracker only worked where the stub had won — against
        # real Home Assistant the genuine scheduler ran and demanded a real
        # ``hass.loop`` off the suite's ``_Hass`` fake. Tests that OBSERVE
        # arming still overwrite these with recording versions.
        A.async_track_point_in_time = lambda hass, action, when: (lambda: None)
        A.async_track_time_change = lambda hass, action, **kw: (lambda: None)

    def tearDown(self):
        (
            A.compute_prayer_times_utc,
            A.async_track_point_in_time,
            A.async_track_time_change,
        ) = self._saved_globals
        super().tearDown()


class TestLibraryCompute(unittest.TestCase):
    """Real library — skipped where prayer-times-calculator-offline isn't installed."""

    def setUp(self):
        try:
            import prayer_times_calculator_offline  # noqa: F401
        except Exception:
            self.skipTest("prayer-times-calculator-offline not installed")

    def test_times_sane(self):
        out = A.compute_prayer_times_utc(24.7136, 46.6753, "makkah", "shafi", "2026-07-03")
        self.assertIsNotNone(out)
        self.assertEqual(set(out), set(A.PRAYER_NAMES))
        for dt in out.values():
            self.assertIsNotNone(dt.tzinfo)  # aware UTC
        # The five daily prayers compute in chronological order.
        times = [out[name] for name in A.PRAYER_NAMES]
        self.assertEqual(times, sorted(times))

    def test_egyptian_alias_maps_to_egypt(self):
        # "egyptian" isn't a library key ("egypt" is) — must not crash/return None.
        out = A.compute_prayer_times_utc(30.0, 31.2, "egyptian", "shafi", "2026-07-03")
        self.assertIsNotNone(out)

    def test_hanafi_asr_later_than_shafi(self):
        shafi = A.compute_prayer_times_utc(24.7136, 46.6753, "makkah", "shafi", "2026-07-03")
        hanafi = A.compute_prayer_times_utc(24.7136, 46.6753, "makkah", "hanafi", "2026-07-03")
        self.assertGreater(hanafi["Asr"], shafi["Asr"])  # Hanafi Asr is later

    def test_unknown_method_falls_back(self):
        out = A.compute_prayer_times_utc(24.7136, 46.6753, "bogus", "shafi", "2026-07-03")
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
            (24.7136, 46.6753, "Asia/Riyadh", "makkah", "shafi"),
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


class TestFireAndArming(_RestoresModuleGlobals, unittest.TestCase):
    def test_fire_builds_broadcast_play(self):
        adapter = _Adapter()
        s = A.AthanScheduler(
            _Hass(),
            _Engine({"enabled": True, "lat": 24.7136, "lon": 46.6753,
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
            _Engine({"enabled": True, "lat": 24.7136, "lon": 46.6753,
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


class TestTargeting(unittest.TestCase):
    """Per-speaker athan targeting and the all / subset / none rule."""

    @staticmethod
    def _engine(selection, enrolled):
        athan = {"enabled": True, "lat": 24.7136, "lon": 46.6753,
                 "timezone": "Asia/Riyadh", "method": "makkah"}
        if selection is not None:
            athan["speakers"] = selection
        return _Engine(athan, speakers=[{"mac6": m} for m in enrolled])

    def test_no_selection_is_broadcast(self):
        eng = self._engine(None, ["aabbcc"])
        has, targets = A.AthanScheduler(_Hass(), eng, _Adapter())._resolve_targets(eng.get_athan())
        self.assertFalse(has)
        self.assertEqual(targets, [])

    def test_selection_intersects_enrolled(self):
        # ddeeff is selected but not enrolled → dropped; order preserved.
        eng = self._engine(["aabbcc", "ddeeff"], ["112233", "aabbcc"])
        has, targets = A.AthanScheduler(_Hass(), eng, _Adapter())._resolve_targets(eng.get_athan())
        self.assertTrue(has)
        self.assertEqual(targets, ["aabbcc"])

    def test_fire_targets_selected_speakers_only(self):
        adapter = _Adapter()
        eng = self._engine(["aabbcc", "ddeeff"], ["aabbcc", "ddeeff", "112233"])
        A.AthanScheduler(_Hass(), eng, adapter)._fire_athan("Asr")
        topics = sorted(t for t, _ in adapter.published)
        self.assertEqual(topics, ["speakers/aabbcc/command", "speakers/ddeeff/command"])
        self.assertNotIn("speakers/broadcast", topics)  # not the third, not all

    def test_fire_no_selection_broadcasts(self):
        adapter = _Adapter()
        eng = self._engine(None, ["aabbcc"])
        A.AthanScheduler(_Hass(), eng, adapter)._fire_athan("Asr")
        self.assertEqual([t for t, _ in adapter.published], ["speakers/broadcast"])

    def test_fire_selection_all_unenrolled_fires_nowhere(self):
        # An explicit selection whose speakers are all gone must NOT fall back
        # to broadcast — athan fires on nothing (never blasts everyone).
        adapter = _Adapter()
        eng = self._engine(["ddeeff"], ["aabbcc"])
        A.AthanScheduler(_Hass(), eng, adapter)._fire_athan("Asr")
        self.assertEqual(adapter.published, [])


class TestScheduleSnapshot(_RestoresModuleGlobals, unittest.TestCase):
    def test_snapshot_reports_next_and_targets(self):
        A.compute_prayer_times_utc = _future_times()
        eng = _Engine(
            {"enabled": True, "lat": 24.7136, "lon": 46.6753,
             "timezone": "Asia/Riyadh", "method": "makkah", "speakers": ["aabbcc"]},
            speakers=[{"mac6": "aabbcc"}],
        )
        s = A.AthanScheduler(_Hass(), eng, _Adapter())
        s.reschedule()
        snap = s.schedule_snapshot()
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["speakers_mode"], "subset")
        self.assertEqual(snap["speakers"], ["aabbcc"])
        self.assertEqual(len(snap["prayers"]), 5)
        self.assertEqual(snap["next"]["name"], "Dhuhr")  # Fajr past, Dhuhr next

    def test_snapshot_disabled_is_minimal(self):
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": False}), _Adapter())
        s.reschedule()
        self.assertEqual(s.schedule_snapshot(), {"enabled": False})


class TestHourlyRearm(_RestoresModuleGlobals, unittest.IsolatedAsyncioTestCase):
    async def test_async_start_registers_hourly_recompute(self):
        calls = []
        A.async_track_time_change = lambda hass, action, **kw: (calls.append(kw) or (lambda: None))
        A.compute_prayer_times_utc = _future_times()
        s = A.AthanScheduler(_Hass(), _Engine({"enabled": False}), _Adapter())
        await s.async_start()
        # Hourly (minute=1, no hour=) so a missed trigger self-heals within the hour.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {"minute": 1, "second": 0})
        self.assertNotIn("hour", calls[0])


if __name__ == "__main__":
    unittest.main()
