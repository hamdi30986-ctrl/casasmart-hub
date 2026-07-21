"""Unit tests for B14 piece 2: the hub-side audio adapter (MQTT/HA glue).

Like the alarm adapter, this is pure glue, so it imports ``homeassistant.*`` at
module top and we inject light stubs into ``sys.modules`` BEFORE importing it.
The MQTT client is a ``_FakePaho`` that records what was published/subscribed
and lets each test drive the on_connect/on_message callbacks by hand — the real
paho network thread never runs. The engine itself is the REAL ``AudioEngine``
over temp storage, so the adapter is exercised against the true engine contract.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

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


# -- homeassistant stubs (installed before importing the adapter) -------------
# Shared stub package (tests/hastubs) — the adapter only needs core
# (HomeAssistant/callback), but every suite installs the same superset so
# load order can never change what a sibling sees.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

import casasmart.audio_adapter as audio_adapter  # noqa: E402
from casasmart.audio_adapter import AudioAdapter, AudioAdapterNotReady  # noqa: E402
from storage import HubStorage  # noqa: E402
from const import EVENT_AUDIO_CHANGED  # noqa: E402
from audio import AudioEngine  # noqa: E402


# -- fakes --------------------------------------------------------------------
class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class _FakeBus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, object]] = []

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))

    def count(self, event_type) -> int:
        return sum(1 for ev, _ in self.fired if ev == event_type)


class _FakeLoop:
    """call_soon_threadsafe runs inline — the test owns the timing."""

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()
        self.loop = _FakeLoop()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakePaho:
    """Records calls; exposes the callbacks the adapter assigns so tests can
    fire on_connect/on_message as the network thread would."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.username = None
        self.password = None
        self.tls = False
        self.connected_to = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.subscriptions: list = []
        self.published: list[tuple[str, object, int, bool]] = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def username_pw_set(self, username, password=None):
        self.username, self.password = username, password

    def tls_set(self, *a, **k):
        self.tls = True

    def reconnect_delay_set(self, **k):
        pass

    def connect_async(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topics):
        self.subscriptions.append(topics)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    # -- test drivers ----------------------------------------------------------
    def fire_connect(self, rc=0):
        self.on_connect(self, None, None, rc)

    def fire_message(self, topic, payload):
        msg = types.SimpleNamespace(topic=topic, payload=payload)
        self.on_message(self, None, msg)


class _Msg(types.SimpleNamespace):
    pass


class AudioAdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)

        self.clock = _Clock()
        self.engine = AudioEngine(
            self.storage.table("audio_config"),
            self.storage.table("audio_speakers"),
            clock=self.clock,
        )
        self.engine.warm_up()

        self.made: list[_FakePaho] = []

        def _factory(client_id):
            client = _FakePaho(client_id)
            self.made.append(client)
            return client

        self.hass = _FakeHass()
        self.adapter = AudioAdapter(self.hass, self.engine, client_factory=_factory)

    @property
    def client(self) -> _FakePaho:
        return self.made[-1]

    # -- start: unconfigured ---------------------------------------------------

    async def test_start_with_no_broker_is_inert(self):
        await self.adapter.async_start()
        self.assertEqual(self.made, [])  # factory never called
        with self.assertRaises(AudioAdapterNotReady):
            self.adapter.publish("speakers/965cb9/command", {"cmd": "stop"})

    # -- start: configured -----------------------------------------------------

    async def test_start_connects_with_creds_and_tls(self):
        self.engine.set_broker(
            host="192.168.8.235", port=8883, tls=True, username="maz", password="pw"
        )
        await self.adapter.async_start()
        self.assertEqual(self.client.connected_to, ("192.168.8.235", 8883))
        self.assertTrue(self.client.loop_started)
        self.assertEqual(self.client.username, "maz")
        self.assertTrue(self.client.tls)

    async def test_start_without_auth_skips_username(self):
        self.engine.set_broker(host="192.168.8.235", port=1883)
        await self.adapter.async_start()
        self.assertIsNone(self.client.username)
        self.assertFalse(self.client.tls)

    async def test_on_connect_subscribes_and_pings(self):
        self.engine.set_broker(host="h", port=1883)
        await self.adapter.async_start()
        self.client.fire_connect(rc=0)
        topics = [t for (t, _q) in self.client.subscriptions[0]]
        self.assertIn("speakers/+/status", topics)
        self.assertIn("speakers/+/state", topics)
        self.assertIn("speakers/announce", topics)
        self.assertTrue(any(p[0] == "speakers/ping" for p in self.client.published))

    async def test_on_connect_failure_does_not_subscribe(self):
        self.engine.set_broker(host="h", port=1883)
        await self.adapter.async_start()
        self.client.fire_connect(rc=5)  # auth rejected
        self.assertEqual(self.client.subscriptions, [])

    # -- M4: stored athan re-published retained on every (re)connect ----------

    async def test_on_connect_republishes_stored_athan_retained(self):
        self.engine.set_broker(host="h", port=1883)
        self.engine.set_athan(
            {"enabled": True, "lat": 21.3, "lon": 39.8, "method": "makkah"}
        )
        await self.adapter.async_start()
        self.client.fire_connect(rc=0)
        athan_pubs = [p for p in self.client.published if p[0] == "athan/config"]
        self.assertEqual(len(athan_pubs), 1)
        topic, body, qos, retain = athan_pubs[0]
        self.assertTrue(retain)
        self.assertEqual(qos, 1)
        self.assertEqual(json.loads(body)["method"], "makkah")

    async def test_on_connect_no_athan_publishes_nothing(self):
        self.engine.set_broker(host="h", port=1883)
        await self.adapter.async_start()
        self.client.fire_connect(rc=0)
        self.assertFalse(any(p[0] == "athan/config" for p in self.client.published))

    async def test_failed_connect_does_not_republish_athan(self):
        self.engine.set_broker(host="h", port=1883)
        self.engine.set_athan({"enabled": True, "lat": 21.3, "lon": 39.8})
        await self.adapter.async_start()
        self.client.fire_connect(rc=5)
        self.assertFalse(any(p[0] == "athan/config" for p in self.client.published))

    # -- M3: clearing a removed speaker's retained ghosts ---------------------

    async def test_clear_speaker_retained_wipes_status_and_state(self):
        await self._started()
        self.adapter.clear_speaker_retained("965cb9")
        cleared = {
            p[0]: p for p in self.client.published if p[0].startswith("speakers/965cb9/")
        }
        self.assertIn("speakers/965cb9/status", cleared)
        self.assertIn("speakers/965cb9/state", cleared)
        for topic in ("speakers/965cb9/status", "speakers/965cb9/state"):
            _t, body, _qos, retain = cleared[topic]
            self.assertEqual(body, "")  # empty payload clears the retained msg
            self.assertTrue(retain)

    async def test_clear_speaker_retained_raises_when_bus_down(self):
        # No start() — client never connected.
        with self.assertRaises(AudioAdapterNotReady):
            self.adapter.clear_speaker_retained("965cb9")

    # -- ingest paths ----------------------------------------------------------

    async def _started(self):
        self.engine.set_broker(host="h", port=1883)
        await self.adapter.async_start()

    async def test_announce_makes_speaker_discoverable(self):
        await self._started()
        self.client.fire_message(
            "speakers/announce", json.dumps({"mac": "965cb9", "room": "Work Room"})
        )
        discovered = self.engine.discovered()
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["mac6"], "965cb9")
        self.assertEqual(discovered[0]["room"], "Work Room")
        self.assertEqual(self.hass.bus.count(EVENT_AUDIO_CHANGED), 1)

    async def test_retained_status_and_state_update_live(self):
        await self._started()
        self.engine.enroll_speaker("965cb9", "Work Room")
        self.client.fire_message("speakers/965cb9/status", "online")
        self.client.fire_message(
            "speakers/965cb9/state", json.dumps({"volume": 42, "playing": True})
        )
        live = self.engine.live_status("965cb9")
        self.assertTrue(live["online"])
        self.assertEqual(live["volume"], 42)
        self.assertTrue(live["playing"])
        self.assertEqual(self.hass.bus.count(EVENT_AUDIO_CHANGED), 2)

    async def test_bytes_payload_is_decoded(self):
        await self._started()
        self.client.fire_message(
            "speakers/965cb9/state", json.dumps({"volume": 10}).encode("utf-8")
        )
        self.assertEqual(self.engine.live_status("965cb9")["volume"], 10)

    async def test_empty_status_payload_is_ignored(self):
        await self._started()
        self.client.fire_message("speakers/965cb9/status", "")
        self.assertEqual(self.hass.bus.count(EVENT_AUDIO_CHANGED), 0)

    async def test_unrelated_topic_is_ignored(self):
        await self._started()
        self.client.fire_message("athan/playback/status", "{}")
        self.assertEqual(self.hass.bus.count(EVENT_AUDIO_CHANGED), 0)

    async def test_malformed_state_json_does_not_crash_or_nudge(self):
        await self._started()
        self.client.fire_message("speakers/965cb9/state", "{not json")
        self.assertEqual(self.hass.bus.count(EVENT_AUDIO_CHANGED), 0)

    # -- outbound --------------------------------------------------------------

    async def test_publish_encodes_dict_payload(self):
        await self._started()
        self.adapter.publish("speakers/965cb9/command", {"cmd": "stop"}, qos=1)
        topic, body, qos, retain = self.client.published[-1]
        self.assertEqual(topic, "speakers/965cb9/command")
        self.assertEqual(json.loads(body), {"cmd": "stop"})
        self.assertEqual(qos, 1)

    async def test_publish_passes_string_through(self):
        await self._started()
        self.adapter.publish("athan/config", "raw", retain=True)
        topic, body, _qos, retain = self.client.published[-1]
        self.assertEqual(body, "raw")
        self.assertTrue(retain)

    async def test_discover_pings_and_returns_unenrolled(self):
        await self._started()
        self.client.fire_message(
            "speakers/965cb9/status", "online"
        )  # heard from, not enrolled
        result = await self.adapter.async_discover()
        self.assertEqual([s["mac6"] for s in result], ["965cb9"])
        self.assertTrue(any(p[0] == "speakers/ping" for p in self.client.published))

    # -- teardown --------------------------------------------------------------

    async def test_stop_tears_down_client_and_is_idempotent(self):
        await self._started()
        captured = self.client
        await self.adapter.async_stop()
        self.assertTrue(captured.loop_stopped)
        self.assertTrue(captured.disconnected)
        # Second stop must not raise (double unload) and not need a client.
        await self.adapter.async_stop()
        with self.assertRaises(AudioAdapterNotReady):
            self.adapter.publish("speakers/965cb9/command", {"cmd": "stop"})

    async def test_reconfigure_cycles_the_connection(self):
        self.engine.set_broker(host="old", port=1883)
        await self.adapter.async_start()
        first = self.client
        self.engine.set_broker(host="new", port=1883)
        await self.adapter.async_reconfigure()
        self.assertTrue(first.loop_stopped)
        self.assertEqual(self.client.connected_to, ("new", 1883))


if __name__ == "__main__":
    unittest.main()
