"""Unit tests for B14: the hub-side audio engine.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage  # noqa: E402
from audio import (  # noqa: E402
    CMD_RESET,
    CMD_STOP,
    CMD_VOLUME,
    TOPIC_BROADCAST,
    AudioEngine,
    AudioError,
    UnknownSpeakerError,
    normalize_mac6,
    speaker_command_topic,
    speaker_airplay_remote_topic,
)


class _Clock:
    """Hand-cranked clock so timestamps are deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class AudioTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.storage.close)
        self.clock = _Clock()
        self.engine = self._make_engine()

    def _make_engine(self) -> AudioEngine:
        engine = AudioEngine(
            self.storage.table("audio_config"),
            self.storage.table("audio_speakers"),
            clock=self.clock,
        )
        engine.warm_up()
        return engine


class NormalizeMacTests(AudioTestCase):
    def test_accepts_colon_mac_takes_last_six(self):
        self.assertEqual(normalize_mac6("DC:A6:32:96:5C:B9"), "965cb9")

    def test_accepts_bare_twelve_hex(self):
        self.assertEqual(normalize_mac6("dca632965cb9"), "965cb9")

    def test_accepts_already_trimmed(self):
        self.assertEqual(normalize_mac6("965CB9"), "965cb9")

    def test_rejects_non_hex(self):
        with self.assertRaises(AudioError):
            normalize_mac6("not-a-mac")

    def test_rejects_too_short(self):
        with self.assertRaises(AudioError):
            normalize_mac6("abc")

    def test_rejects_empty(self):
        with self.assertRaises(AudioError):
            normalize_mac6("")


class BrokerConfigTests(AudioTestCase):
    def test_default_broker(self):
        broker = self.engine.get_broker()
        self.assertEqual(broker["port"], 1883)
        self.assertFalse(broker["tls"])
        self.assertIsNone(broker["host"])

    def test_set_and_persist_broker(self):
        self.engine.set_broker(
            host="192.168.8.235", port=8883, tls=True, username="maz", password="s3cr3t"
        )
        # New engine off the same tables must see the persisted values.
        fresh = self._make_engine()
        broker = fresh.get_broker()
        self.assertEqual(broker["host"], "192.168.8.235")
        self.assertEqual(broker["port"], 8883)
        self.assertTrue(broker["tls"])
        self.assertEqual(broker["username"], "maz")
        self.assertEqual(broker["password"], "s3cr3t")

    def test_partial_update_keeps_other_fields(self):
        self.engine.set_broker(host="a", username="u", password="p")
        self.engine.set_broker(host="b")
        broker = self.engine.get_broker()
        self.assertEqual(broker["host"], "b")
        self.assertEqual(broker["username"], "u")  # untouched

    def test_bad_port_rejected(self):
        with self.assertRaises(AudioError):
            self.engine.set_broker(port=99999)

    def test_provision_returns_broker_coords(self):
        self.engine.set_broker(
            host="192.168.8.235", port=1883, username="maz", password="p"
        )
        prov = self.engine.provision()
        self.assertEqual(prov["host"], "192.168.8.235")
        self.assertEqual(prov["username"], "maz")
        self.assertEqual(prov["password"], "p")


class PaConfigTests(AudioTestCase):
    def test_default_pa_port(self):
        self.assertEqual(self.engine.get_pa()["port"], 9876)

    def test_set_and_persist_pa(self):
        self.engine.set_pa(host="192.168.8.235", port=9876, api_key="k")
        fresh = self._make_engine()
        pa = fresh.get_pa()
        self.assertEqual(pa["host"], "192.168.8.235")
        self.assertEqual(pa["api_key"], "k")


class AthanConfigTests(AudioTestCase):
    def test_set_and_persist_athan(self):
        cfg = {
            "enabled": True,
            "method": "makkah",
            "volume": 60,
            "lat": 21.3891,
            "lon": 39.8579,
        }
        self.engine.set_athan(cfg)
        fresh = self._make_engine()
        self.assertEqual(fresh.get_athan(), cfg)

    def test_rejects_non_object(self):
        with self.assertRaises(AudioError):
            self.engine.set_athan([1, 2, 3])

    def test_rejects_non_serialisable(self):
        with self.assertRaises(AudioError):
            self.engine.set_athan({"bad": {1, 2, 3}})  # a set isn't JSON

    # -- athan coordinates are OPTIONAL (hub-inherited location) ---------------

    def test_enabled_without_coords_allowed(self):
        # The hub-native scheduler falls back to the home's own location, so an
        # enabled athan with no coords is valid (the app inherits location now).
        cfg = {"enabled": True, "method": "makkah"}
        self.engine.set_athan(cfg)
        self.assertEqual(self.engine.get_athan(), cfg)

    def test_enabled_with_null_coords_allowed(self):
        cfg = {"enabled": True, "lat": None, "lon": None, "method": "makkah"}
        self.engine.set_athan(cfg)
        self.assertEqual(self.engine.get_athan(), cfg)

    def test_enabled_with_bool_coords_rejected(self):
        # bool is an int subclass — must not pass as a coordinate if supplied.
        with self.assertRaises(AudioError):
            self.engine.set_athan({"enabled": True, "lat": True, "lon": True})

    def test_enabled_with_non_numeric_coord_rejected(self):
        # A supplied coordinate that isn't a finite number is still refused.
        with self.assertRaises(AudioError):
            self.engine.set_athan({"enabled": True, "lat": "nope", "lon": 39.0})

    def test_disabled_without_coords_allowed(self):
        # Turning athan OFF must always persist, coords or not.
        cfg = {"enabled": False, "method": "makkah"}
        self.engine.set_athan(cfg)
        self.assertEqual(self.engine.get_athan(), cfg)

    def test_enabled_with_int_coords_allowed(self):
        cfg = {"enabled": True, "lat": 21, "lon": 39}
        self.engine.set_athan(cfg)
        self.assertEqual(self.engine.get_athan(), cfg)

    def test_rejects_oversized(self):
        with self.assertRaises(AudioError):
            self.engine.set_athan({"blob": "x" * 9000})

    def test_rejects_non_bool_enabled(self):
        with self.assertRaises(AudioError):
            self.engine.set_athan({"enabled": 1, "lat": 21, "lon": 39})

    def test_rejects_enabled_with_non_finite_coords(self):
        with self.assertRaises(AudioError):
            self.engine.set_athan(
                {"enabled": True, "lat": float("inf"), "lon": 39}
            )

    def test_preserves_unknown_scheduler_keys(self):
        # The blob is opaque — keys the hub doesn't model must round-trip.
        cfg = {"enabled": True, "lat": 21, "lon": 39, "per_prayer": {"fajr": 5}}
        self.engine.set_athan(cfg)
        self.assertEqual(self.engine.get_athan(), cfg)


class SpeakerRegistryTests(AudioTestCase):
    def test_enroll_and_list(self):
        self.engine.enroll_speaker("DC:A6:32:96:5C:B9", "Work Room", room="Office")
        speakers = self.engine.speakers()
        self.assertEqual(len(speakers), 1)
        self.assertEqual(speakers[0]["mac6"], "965cb9")
        self.assertEqual(speakers[0]["name"], "Work Room")
        self.assertEqual(speakers[0]["room"], "Office")
        self.assertEqual(speakers[0]["enrolled_at"], 1000.0)
        # No status heard yet -> offline.
        self.assertFalse(speakers[0]["live"]["online"])

    def test_enroll_persists(self):
        self.engine.enroll_speaker("965cb9", "Work Room")
        fresh = self._make_engine()
        self.assertTrue(fresh.is_enrolled("965cb9"))

    def test_reenroll_preserves_enrolled_at(self):
        self.engine.enroll_speaker("965cb9", "Old")
        self.clock.advance(500)
        rec = self.engine.enroll_speaker("965cb9", "New")
        self.assertEqual(rec["name"], "New")
        self.assertEqual(rec["enrolled_at"], 1000.0)  # original time kept

    def test_update_speaker(self):
        self.engine.enroll_speaker("965cb9", "Work Room")
        rec = self.engine.update_speaker("965cb9", name="Living Room", room="Lounge")
        self.assertEqual(rec["name"], "Living Room")
        self.assertEqual(rec["room"], "Lounge")

    def test_update_unknown_speaker_raises(self):
        with self.assertRaises(UnknownSpeakerError):
            self.engine.update_speaker("aaaaaa", name="x")

    def test_remove_speaker(self):
        self.engine.enroll_speaker("965cb9", "Work Room")
        self.engine.remove_speaker("965cb9")
        self.assertFalse(self.engine.is_enrolled("965cb9"))
        self.assertEqual(self.engine.speakers(), [])

    def test_remove_unknown_raises(self):
        with self.assertRaises(UnknownSpeakerError):
            self.engine.remove_speaker("aaaaaa")

    def test_enroll_requires_name(self):
        with self.assertRaises(AudioError):
            self.engine.enroll_speaker("965cb9", "")


class LiveStatusTests(AudioTestCase):
    def setUp(self):
        super().setUp()
        self.engine.enroll_speaker("965cb9", "Work Room")

    def test_ingest_status_online_string(self):
        self.engine.ingest_status("965cb9", "online")
        self.assertTrue(self.engine.live_status("965cb9")["online"])

    def test_ingest_status_offline_string(self):
        self.engine.ingest_status("965cb9", "online")
        self.engine.ingest_status("965cb9", "offline")
        self.assertFalse(self.engine.live_status("965cb9")["online"])

    def test_ingest_state_copies_fields(self):
        self.engine.ingest_state(
            "965cb9",
            {
                "volume": 42,
                "playing": True,
                "room": "Work Room",
                "airplay_active": False,
                "uptime": 1234,
            },
        )
        live = self.engine.live_status("965cb9")
        self.assertTrue(live["online"])
        self.assertEqual(live["volume"], 42)
        self.assertTrue(live["playing"])
        self.assertEqual(live["last_seen"], 1000.0)

    def test_ingest_state_ignores_malformed(self):
        self.engine.ingest_state("965cb9", "not-a-dict")
        self.assertFalse(self.engine.live_status("965cb9").get("online", False))

    def test_ingest_state_drops_non_finite_and_wrong_types(self):
        # A bad broker blob must not poison the served mirror (Phase 7).
        self.engine.ingest_state(
            "965cb9",
            {"volume": float("nan"), "playing": "yes", "room": "Office"},
        )
        live = self.engine.live_status("965cb9")
        self.assertNotIn("volume", live)  # NaN dropped
        self.assertNotIn("playing", live)  # wrong type dropped
        self.assertEqual(live["room"], "Office")  # valid value kept

    def test_live_status_surfaces_in_list(self):
        self.engine.ingest_state("965cb9", {"volume": 30, "playing": False})
        speakers = self.engine.speakers()
        self.assertEqual(speakers[0]["live"]["volume"], 30)

    def test_remove_clears_live(self):
        self.engine.ingest_status("965cb9", "online")
        self.engine.remove_speaker("965cb9")
        # A re-enrolled speaker starts fresh, not online from a stale blob.
        self.engine.enroll_speaker("965cb9", "Work Room")
        self.assertFalse(self.engine.live_status("965cb9")["online"])


class DiscoveryTests(AudioTestCase):
    def test_announce_marks_online_and_records_room(self):
        self.engine.ingest_announce("965cb9", "Work Room")
        live = self.engine.live_status("965cb9")
        self.assertTrue(live["online"])
        self.assertEqual(live["room"], "Work Room")

    def test_announce_without_room_is_fine(self):
        self.engine.ingest_announce("965cb9")
        self.assertTrue(self.engine.live_status("965cb9")["online"])

    def test_discovered_lists_only_unenrolled(self):
        self.engine.ingest_announce("965cb9", "Work Room")  # not enrolled
        self.engine.enroll_speaker("aabbcc", "Living")  # enrolled
        self.engine.ingest_status("aabbcc", "online")
        discovered = self.engine.discovered()
        self.assertEqual([s["mac6"] for s in discovered], ["965cb9"])

    def test_enrolling_moves_out_of_discovered(self):
        self.engine.ingest_announce("965cb9", "Work Room")
        self.assertEqual(len(self.engine.discovered()), 1)
        self.engine.enroll_speaker("965cb9", "Work Room")
        self.assertEqual(self.engine.discovered(), [])

    def test_discovered_carries_live_fields(self):
        self.engine.ingest_state("965cb9", {"volume": 55})
        discovered = self.engine.discovered()
        self.assertEqual(discovered[0]["volume"], 55)
        self.assertTrue(discovered[0]["online"])

    # -- M6: stale ghosts age out of the discover list ------------------------

    def test_stale_ghost_filtered_out(self):
        self.engine.ingest_announce("965cb9", "Work Room")  # seen at t=1000
        self.clock.advance(601)  # past the 600s TTL
        self.assertEqual(self.engine.discovered(), [])

    def test_fresh_ghost_kept(self):
        self.engine.ingest_announce("965cb9", "Work Room")
        self.clock.advance(599)  # still inside the TTL
        self.assertEqual([s["mac6"] for s in self.engine.discovered()], ["965cb9"])

    def test_ttl_none_keeps_everything(self):
        self.engine.ingest_announce("965cb9", "Work Room")
        self.clock.advance(10_000)
        self.assertEqual(
            [s["mac6"] for s in self.engine.discovered(ttl=None)], ["965cb9"]
        )

    def test_recent_announce_refreshes_freshness(self):
        self.engine.ingest_announce("965cb9", "Work Room")  # t=1000
        self.clock.advance(500)
        self.engine.ingest_announce("965cb9", "Work Room")  # re-seen at t=1500
        self.clock.advance(400)  # 400s since last seen, < TTL
        self.assertEqual([s["mac6"] for s in self.engine.discovered()], ["965cb9"])


class CommandTests(AudioTestCase):
    def setUp(self):
        super().setUp()
        self.engine.enroll_speaker("965cb9", "Work Room")

    def test_volume_command_shape(self):
        topic, payload = self.engine.build_command("965cb9", CMD_VOLUME, value=40)
        self.assertEqual(topic, speaker_command_topic("965cb9"))
        self.assertEqual(payload["cmd"], "volume")
        self.assertEqual(payload["value"], 40)
        self.assertEqual(payload["ts"], 1000.0)  # stale-guard timestamp

    def test_stop_command_shape(self):
        topic, payload = self.engine.build_command("965cb9", CMD_STOP)
        self.assertEqual(payload, {"cmd": "stop"})

    def test_reset_command_shape(self):
        _, payload = self.engine.build_command("965cb9", CMD_RESET)
        self.assertEqual(payload, {"cmd": "reset"})

    def test_volume_out_of_range_rejected(self):
        with self.assertRaises(AudioError):
            self.engine.build_command("965cb9", CMD_VOLUME, value=150)

    def test_volume_non_int_rejected(self):
        with self.assertRaises(AudioError):
            self.engine.build_command("965cb9", CMD_VOLUME, value="loud")

    def test_unknown_command_rejected(self):
        with self.assertRaises(AudioError):
            self.engine.build_command("965cb9", "explode")

    def test_play_not_a_control_command(self):
        # 'play' must never come through the control path (no file smuggling).
        with self.assertRaises(AudioError):
            self.engine.build_command("965cb9", "play")

    def test_command_on_unknown_speaker_raises(self):
        with self.assertRaises(UnknownSpeakerError):
            self.engine.build_command("aaaaaa", CMD_STOP)


class AirplayRemoteTests(AudioTestCase):
    def setUp(self):
        super().setUp()
        self.engine.enroll_speaker("965cb9", "Work Room")

    def test_playpause_maps_to_verb_and_remote_topic(self):
        topic, verb = self.engine.build_airplay_remote("965cb9", "playpause")
        self.assertEqual(topic, speaker_airplay_remote_topic("965cb9"))
        self.assertEqual(topic, "speakers/965cb9/airplay/remote")
        self.assertEqual(verb, "playpause")

    def test_next_previous_map_to_dacp_verbs(self):
        self.assertEqual(
            self.engine.build_airplay_remote("965cb9", "next")[1], "nextitem"
        )
        self.assertEqual(
            self.engine.build_airplay_remote("965cb9", "previous")[1], "previtem"
        )

    def test_verb_is_a_bare_string_not_a_dict(self):
        # shairport's remote topic wants a raw command word, never JSON.
        _, verb = self.engine.build_airplay_remote("965cb9", "pause")
        self.assertIsInstance(verb, str)

    def test_unknown_action_rejected(self):
        with self.assertRaises(AudioError):
            self.engine.build_airplay_remote("965cb9", "moonwalk")

    def test_airplay_on_unknown_speaker_raises(self):
        with self.assertRaises(UnknownSpeakerError):
            self.engine.build_airplay_remote("aaaaaa", "playpause")


class PlayBuildTests(AudioTestCase):
    def setUp(self):
        super().setUp()
        self.engine.enroll_speaker("965cb9", "Work Room")

    def test_broadcast_play_url(self):
        topic, payload = self.engine.build_play(url="http://h/pa.mp3", priority="pa")
        self.assertEqual(topic, TOPIC_BROADCAST)
        self.assertEqual(payload["cmd"], "play")
        self.assertEqual(payload["url"], "http://h/pa.mp3")
        self.assertEqual(payload["priority"], "pa")

    def test_targeted_play_file(self):
        topic, payload = self.engine.build_play(mac="965cb9", file="/srv/athan.mp3")
        self.assertEqual(topic, speaker_command_topic("965cb9"))
        self.assertEqual(payload["file"], "/srv/athan.mp3")

    def test_play_requires_exactly_one_source(self):
        with self.assertRaises(AudioError):
            self.engine.build_play()  # neither
        with self.assertRaises(AudioError):
            self.engine.build_play(url="a", file="b")  # both

    def test_targeted_play_unknown_speaker_raises(self):
        with self.assertRaises(UnknownSpeakerError):
            self.engine.build_play(mac="aaaaaa", url="http://h/x.mp3")

    def test_play_volume_validated(self):
        with self.assertRaises(AudioError):
            self.engine.build_play(url="http://h/x.mp3", volume=200)

    def test_play_rejects_unknown_priority(self):
        with self.assertRaises(AudioError):
            self.engine.build_play(url="http://h/x.mp3", priority="bogus")


if __name__ == "__main__":
    unittest.main()
