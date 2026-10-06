"""Hub-side audio adapter (Phase 6, block B14) — the MQTT/HA glue.

The pure decision+state engine lives in ``audio.py`` (stdlib only, no network,
unit-tested on a temp DB). This module is everything that engine deliberately
does NOT do: it is the hub's **single, only** MQTT client — the whole point of
B14 is that the phone stops holding broker creds and opening its own
``MqttServerClient``; the hub owns the one connection and the phone just reads
hub state over REST/WS.

What it does:

- Connects to the broker with the **engine's** stored creds (``get_broker``),
  so the credentials live in exactly one place (the hub) instead of in every
  phone's ``SharedPreferences``.
- Subscribes the three speaker topics and feeds them into the engine:
  ``speakers/announce`` -> ``ingest_announce`` (discovery beacon),
  ``speakers/+/status`` -> ``ingest_status`` (retained online/offline),
  ``speakers/+/state``  -> ``ingest_state``  (retained JSON snapshot).
  Because status/state are retained, a fresh connection replays the current
  truth of every speaker — the live mirror rebuilds itself on every reconnect.
- On every ingested change, nudges the WS server by firing
  ``EVENT_AUDIO_CHANGED`` on the HA loop (the ingest itself runs on paho's
  network thread; the engine is ``RLock``-guarded so that's safe, but the bus
  fire must hop to the loop thread).
- Provides ``publish`` for the REST API (B14 piece 3) to send the topic+payload
  the engine's ``build_command`` / ``build_play`` produced, and ``async_discover``
  to provoke a fresh round of announces (publishes ``speakers/ping``).

Discovery is MQTT, NOT an HTTP subnet sweep: the real Pi agent has no HTTP
health endpoint — it announces over the broker (on connect + on ``ping``). So
the hub, being the only MQTT client, is also the only thing that needs to do
discovery, and it does it by listening for announces + pinging.

Graceful when unconfigured: a hub with no broker host set (fresh install,
before the installer provisions the broker) starts inert and logs it — setup
must never fail just because audio isn't configured yet. ``async_reconfigure``
brings the connection up (or cycles it) once the creds are written.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

from homeassistant.core import HomeAssistant, callback

from .audio import (
    AudioEngine,
    TOPIC_ATHAN_CONFIG,
    speaker_state_topic,
    speaker_status_topic,
)
from .const import EVENT_AUDIO_CHANGED

_LOGGER = logging.getLogger(__name__)


class AudioAdapterNotReady(RuntimeError):
    """Raised when a publish is attempted with no live MQTT connection.

    The REST API maps this to a 503 — the control was rejected, not silently
    dropped, so the app can tell the user the speaker bus is unreachable.
    """


# The hub is one client; a stable id keeps the broker's session bookkeeping sane
# across reconnects.
_CLIENT_ID = "casasmart-hub"

# Topics the hub subscribes (the agent's vocabulary, verbatim).
_TOPIC_ANNOUNCE = "speakers/announce"
_TOPIC_PING = "speakers/ping"
_SUB_STATUS = "speakers/+/status"
_SUB_STATE = "speakers/+/state"
# Pull mac6 out of ``speakers/<mac6>/status|state``.
_TOPIC_RE = re.compile(r"^speakers/([0-9a-fA-F]+)/(status|state)$")

# paho auto-reconnect backoff once a connection has been established once.
_RECONNECT_MIN_DELAY = 1
_RECONNECT_MAX_DELAY = 60


def _build_paho_client(client_id: str) -> Any:
    """Default MQTT client factory (real paho). Injected/overridden in tests.

    Handles both paho 1.x and 2.x: 2.x requires an explicit callback-API
    version, and we pin VERSION1 so the ``on_connect``/``on_message`` signatures
    are stable regardless of the installed paho (the Pi agent does the same).
    """
    import paho.mqtt.client as mqtt  # lazy — keeps import cost off non-audio hubs

    if hasattr(mqtt, "CallbackAPIVersion"):  # paho 2.x
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
            clean_session=True,
        )
    return mqtt.Client(client_id=client_id, clean_session=True)  # paho 1.x


class AudioAdapter:
    """Bridges the pure ``AudioEngine`` to the live MQTT broker + HA bus."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: AudioEngine,
        *,
        client_factory: Callable[[str], Any] = _build_paho_client,
    ) -> None:
        self._hass = hass
        self._engine = engine
        self._client_factory = client_factory
        self._client: Optional[Any] = None
        # True between a successful connect-attempt setup and stop; lets
        # publish/ discover fail fast (and loudly) when audio isn't wired.
        self._started = False

    # -- lifecycle -------------------------------------------------------------

    async def async_start(self) -> None:
        """Bring up the MQTT connection from the engine's stored broker creds.

        No-op (logged) when no broker host is configured — a hub that hasn't
        been provisioned yet must still finish setup. ``connect_async`` +
        ``loop_start`` never block the event loop: the network thread owns the
        actual TCP connect and all reconnects.
        """
        broker = self._engine.get_broker()
        host = broker.get("host")
        if not host:
            _LOGGER.info(
                "CasaSmart audio: no broker configured — MQTT client inert "
                "until provisioned"
            )
            return

        client = self._client_factory(_CLIENT_ID)
        username = broker.get("username")
        if username:
            client.username_pw_set(username, broker.get("password"))
        if broker.get("tls"):
            client.tls_set()
        client.reconnect_delay_set(
            min_delay=_RECONNECT_MIN_DELAY, max_delay=_RECONNECT_MAX_DELAY
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        self._client = client
        self._started = True
        port = broker.get("port") or 1883
        try:
            client.connect_async(host, port)
            client.loop_start()
        except Exception:  # noqa: BLE001 — a bad broker must not abort hub setup
            _LOGGER.exception(
                "CasaSmart audio: failed to start MQTT to %s:%s", host, port
            )
            self._client = None
            self._started = False
            return
        _LOGGER.info("CasaSmart audio: MQTT client connecting to %s:%s", host, port)

    async def async_stop(self) -> None:
        """Stop the network thread and disconnect (idempotent)."""
        client = self._client
        self._client = None
        self._started = False
        if client is None:
            return
        # loop_stop joins the network thread — do it off the event loop.
        await self._hass.async_add_executor_job(self._teardown_client, client)

    @staticmethod
    def _teardown_client(client: Any) -> None:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001 — teardown must never raise on unload
            _LOGGER.exception("CasaSmart audio: error stopping MQTT client")

    async def async_reconfigure(self) -> None:
        """Cycle the connection to pick up changed broker creds (API hook)."""
        await self.async_stop()
        await self.async_start()

    # -- MQTT callbacks (run on paho's network thread) -------------------------

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, rc: Any) -> None:
        """Subscribe + provoke a discovery round on every (re)connect.

        Retained ``status``/``state`` are replayed by the broker right after
        the subscribe, so the engine's live mirror rebuilds itself here without
        any explicit "fetch current speakers" call. The ping nudges any online
        speaker that has no retained announce to re-announce.
        """
        if rc != 0:
            _LOGGER.warning("CasaSmart audio: MQTT connect failed (rc=%s)", rc)
            return
        client.subscribe(
            [(_TOPIC_ANNOUNCE, 0), (_SUB_STATUS, 1), (_SUB_STATE, 0)]
        )
        client.publish(_TOPIC_PING, "", qos=0)
        # Re-push the stored athan config retained on every (re)connect (M4).
        # The PUT relays it once when the bus is up, but a config saved while
        # the bus was down — or a scheduler that restarted and lost its retained
        # copy — would otherwise silently never see it. Re-publishing here makes
        # the hub the durable source of truth: connect == scheduler is current.
        self._republish_athan(client)
        _LOGGER.info("CasaSmart audio: MQTT connected, subscribed to speaker topics")

    def _republish_athan(self, client: Any) -> None:
        """Publish the engine's stored athan config retained (best-effort)."""
        try:
            athan = self._engine.get_athan()
            if athan:
                client.publish(
                    TOPIC_ATHAN_CONFIG, json.dumps(athan), qos=1, retain=True
                )
        except Exception:  # noqa: BLE001 — a relay failure must not break connect
            _LOGGER.exception("CasaSmart audio: failed to re-publish athan config")

    def _on_disconnect(self, _client: Any, _userdata: Any, rc: Any) -> None:
        # rc 0 = our own disconnect(); anything else = unexpected drop, paho's
        # loop will auto-reconnect on the backoff schedule.
        if rc not in (0, None):
            _LOGGER.warning("CasaSmart audio: MQTT dropped (rc=%s), reconnecting", rc)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Route one broker message into the engine, then nudge the WS server."""
        try:
            changed = self._ingest(message.topic, message.payload)
        except Exception:  # noqa: BLE001 — a bad message must not kill the thread
            _LOGGER.exception(
                "CasaSmart audio: failed to ingest %s", getattr(message, "topic", "?")
            )
            return
        if changed:
            self._nudge_changed()

    def _ingest(self, topic: str, payload: Any) -> bool:
        """Apply a message to the engine. Returns True if it moved live state."""
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload

        if topic == _TOPIC_ANNOUNCE:
            data = self._loads(text)
            if not isinstance(data, dict) or not data.get("mac"):
                return False
            self._engine.ingest_announce(data["mac"], data.get("room"))
            return True

        match = _TOPIC_RE.match(topic)
        if match is None:
            return False
        mac6, kind = match.group(1), match.group(2)
        if kind == "status":
            # Retained empty payload = the broker clearing the topic; ignore.
            if text is None or text == "":
                return False
            self._engine.ingest_status(mac6, text)
            return True
        # state
        data = self._loads(text)
        if not isinstance(data, dict):
            return False
        self._engine.ingest_state(mac6, data)
        return True

    @staticmethod
    def _loads(text: Any) -> Any:
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    @callback
    def _fire_changed(self) -> None:
        self._hass.bus.async_fire(EVENT_AUDIO_CHANGED, None)

    def _nudge_changed(self) -> None:
        """Fire EVENT_AUDIO_CHANGED on the loop thread (we're on paho's)."""
        self._hass.loop.call_soon_threadsafe(self._fire_changed)

    # -- outbound (used by the REST API, B14 piece 3) --------------------------

    def publish(
        self, topic: str, payload: Any, *, qos: int = 1, retain: bool = False
    ) -> None:
        """Publish a command/PA/athan message the engine built.

        ``payload`` may be a dict (JSON-encoded) or a raw string. Raises if the
        client isn't connected so the API can surface a clean 503 instead of
        silently dropping a control the user thinks went through.
        """
        if not self._started or self._client is None:
            raise AudioAdapterNotReady("Audio MQTT client is not connected")
        body = json.dumps(payload) if not isinstance(payload, (str, bytes)) else payload
        self._client.publish(topic, body, qos=qos, retain=retain)

    def clear_speaker_retained(self, mac6: str) -> None:
        """Wipe a removed speaker's retained ``status``/``state`` topics (M3).

        Publishing an empty retained payload tells the broker to drop the
        retained message, so a deleted speaker can't resurrect as a discovery
        ghost the next time the hub reconnects and the broker replays retained
        state. Raises ``AudioAdapterNotReady`` if the bus is down — the DELETE
        handler treats that as a non-fatal best-effort cleanup.
        """
        if not self._started or self._client is None:
            raise AudioAdapterNotReady("Audio MQTT client is not connected")
        for topic in (speaker_status_topic(mac6), speaker_state_topic(mac6)):
            self._client.publish(topic, "", qos=1, retain=True)

    async def async_discover(self) -> list[dict[str, Any]]:
        """Provoke a fresh announce round and return un-enrolled speakers.

        Retained status/state already populate the engine on connect, so the
        snapshot is meaningful immediately; the ping refreshes announces from
        any currently-online speaker.
        """
        if self._started and self._client is not None:
            self._client.publish(_TOPIC_PING, "", qos=0)
        return self._engine.discovered()
