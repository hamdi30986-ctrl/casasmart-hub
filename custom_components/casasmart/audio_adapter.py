"""The hub's MQTT connection to the speakers.

Connects with the broker settings stored in AudioEngine and feeds the
speakers' announce, status and state messages into the engine, firing
EVENT_AUDIO_CHANGED on the HA loop. Status and state are retained, so every
reconnect replays the current state of every speaker. Discovery is MQTT too:
the speaker agent has no HTTP endpoint, so the hub pings and listens for
announces. With no broker configured the adapter stays idle and setup still
succeeds; async_reconfigure connects once the settings are saved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.ssl import client_context

from .audio import (
    TOPIC_ATHAN_CONFIG,
    AudioEngine,
    AudioError,
    speaker_state_topic,
    speaker_status_topic,
)
from .const import EVENT_AUDIO_CHANGED

_LOGGER = logging.getLogger(__name__)


# A fixed client id, so the broker sees a reconnect as the same client.
_CLIENT_ID = "casasmart-hub"

# Speaker agent topics.
_TOPIC_ANNOUNCE = "speakers/announce"
_TOPIC_PING = "speakers/ping"
_SUB_STATUS = "speakers/+/status"
_SUB_STATE = "speakers/+/state"
# Pulls mac6 out of speakers/<mac6>/status or .../state.
_TOPIC_RE = re.compile(r"^speakers/([0-9a-fA-F]+)/(status|state)$")

# paho's auto-reconnect backoff, in seconds.
_RECONNECT_MIN_DELAY = 1
_RECONNECT_MAX_DELAY = 60


class AudioAdapterNotReady(RuntimeError):
    """The speaker bus can't take a publish right now.

    No broker is configured, the client failed to start or was stopped, or it
    isn't connected. The API answers 503, so the app knows the command was
    refused rather than queued.
    """


def _build_paho_client(client_id: str) -> Any:
    """Create a paho client (tests inject a fake).

    paho 2.x needs an explicit callback API version. VERSION1 keeps the
    callback signatures the same on 1.x and 2.x, as the speaker agent does.
    """
    if hasattr(mqtt, "CallbackAPIVersion"):  # paho 2.x
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
            clean_session=True,
        )
    return mqtt.Client(client_id=client_id, clean_session=True)  # paho 1.x


class AudioAdapter:
    """Connects AudioEngine to the MQTT broker and the HA event bus."""

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
        self._client: Any | None = None
        # True between a successful start and stop.
        self._started = False
        # One at a time: a second reconfigure during a stop would start another
        # client, and two clients with one id knock each other off the broker.
        self._reconfigure_lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------

    async def async_start(self) -> None:
        """Connect using the broker settings stored in the engine.

        Does nothing (and logs it) when no broker host is set, so a hub that
        isn't provisioned yet still finishes setup. connect_async and
        loop_start don't block; paho's network thread connects and reconnects.
        """
        broker = self._engine.get_broker()
        host = broker.get("host")
        if not host:
            _LOGGER.info(
                "CasaSmart audio: no broker configured; MQTT client inert "
                "until provisioned"
            )
            return

        client = self._client_factory(_CLIENT_ID)
        username = broker.get("username")
        if username:
            client.username_pw_set(username, broker.get("password"))
        if broker.get("tls"):
            # tls_set() would load the CA certificates on the event loop; HA's
            # shared client context is built once, at import.
            client.tls_set_context(client_context())
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
        except Exception:
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
        # loop_stop joins the network thread, so run it in the executor.
        await self._hass.async_add_executor_job(self._teardown_client, client)

    @staticmethod
    def _teardown_client(client: Any) -> None:
        """Stop paho's network thread and disconnect (blocking; executor)."""
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            _LOGGER.exception("CasaSmart audio: error stopping MQTT client")

    async def async_reconfigure(self) -> None:
        """Reconnect with the current broker settings."""
        async with self._reconfigure_lock:
            await self.async_stop()
            await self.async_start()

    # -- MQTT callbacks (run on paho's network thread) -------------------------

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, rc: Any) -> None:
        """Subscribe and ping the speakers on every (re)connect.

        The broker replays retained status and state right after the
        subscribe, which rebuilds the engine's live status. The ping makes
        online speakers announce themselves.
        """
        if rc != 0:
            _LOGGER.warning("CasaSmart audio: MQTT connect failed (rc=%s)", rc)
            return
        client.subscribe([(_TOPIC_ANNOUNCE, 0), (_SUB_STATUS, 1), (_SUB_STATE, 0)])
        client.publish(_TOPIC_PING, "", qos=0)
        # Covers a config saved while the bus was down, or a broker that lost
        # its retained copy.
        self._republish_athan(client)
        _LOGGER.info("CasaSmart audio: MQTT connected, subscribed to speaker topics")

    def _republish_athan(self, client: Any) -> None:
        """Publish the stored athan config, retained (best effort)."""
        try:
            athan = self._engine.get_athan()
            if athan:
                client.publish(
                    TOPIC_ATHAN_CONFIG, json.dumps(athan), qos=1, retain=True
                )
        except Exception:
            _LOGGER.exception("CasaSmart audio: failed to re-publish athan config")

    def _on_disconnect(self, _client: Any, _userdata: Any, rc: Any) -> None:
        """Log an unexpected drop; paho reconnects on the backoff schedule."""
        # rc 0 is our own disconnect().
        if rc not in (0, None):
            _LOGGER.warning("CasaSmart audio: MQTT dropped (rc=%s), reconnecting", rc)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Route one broker message into the engine, then nudge the WS server."""
        try:
            changed = self._ingest(message.topic, message.payload)
        except AudioError as err:
            # A bad speaker id is bad bus data, not a hub fault: no traceback.
            _LOGGER.debug("CasaSmart audio: ignored %s (%s)", message.topic, err)
            return
        except Exception:
            _LOGGER.exception(
                "CasaSmart audio: failed to ingest %s", getattr(message, "topic", "?")
            )
            return
        if changed:
            self._nudge_changed()

    def _ingest(self, topic: str, payload: Any) -> bool:
        """Apply a message to the engine. Returns True if it moved live state."""
        text = (
            payload.decode("utf-8", "replace")
            if isinstance(payload, bytes)
            else payload
        )

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
            # An empty retained payload means the topic was cleared.
            if text is None or text == "":
                return False
            self._engine.ingest_status(mac6, text)
            return True
        data = self._loads(text)
        if not isinstance(data, dict):
            return False
        self._engine.ingest_state(mac6, data)
        return True

    @staticmethod
    def _loads(text: Any) -> Any:
        """Parse a JSON payload, or None when it is empty or malformed."""
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    @callback
    def _fire_changed(self) -> None:
        """Fire EVENT_AUDIO_CHANGED (loop thread only)."""
        self._hass.bus.async_fire(EVENT_AUDIO_CHANGED, None)

    def _nudge_changed(self) -> None:
        """Fire EVENT_AUDIO_CHANGED from paho's thread via the event loop."""
        self._hass.loop.call_soon_threadsafe(self._fire_changed)

    # -- outbound (REST API and athan scheduler) -------------------------------

    def _running_client(self) -> Any:
        """The client if the adapter is running, else AudioAdapterNotReady.

        While the link is down paho queues a QoS 1 publish and sends it after
        reconnecting, maybe minutes later.
        """
        client = self._client
        if not self._started or client is None:
            raise AudioAdapterNotReady("Audio MQTT client is not connected")
        return client

    def _connected_client(self) -> Any:
        """The client if it is running and connected, else AudioAdapterNotReady.

        A late play or volume change is worse than a refused one.
        """
        client = self._running_client()
        if not client.is_connected():
            raise AudioAdapterNotReady(
                "Speaker bus unavailable: the hub can't reach the MQTT broker"
            )
        return client

    def publish(
        self,
        topic: str,
        payload: Any,
        *,
        qos: int = 1,
        retain: bool = False,
        queue_if_down: bool = False,
    ) -> None:
        """Publish a message the engine built; a dict is sent as JSON.

        Raises AudioAdapterNotReady when not connected, so the API can answer
        503 rather than drop or delay the command. A link that drops between
        the check and the send is left to paho to retry. With queue_if_down,
        for a message that may arrive late (a removed speaker's reset), only
        a stopped adapter raises and paho holds the message for the reconnect.
        """
        client = self._running_client() if queue_if_down else self._connected_client()
        body = json.dumps(payload) if not isinstance(payload, (str, bytes)) else payload
        client.publish(topic, body, qos=qos, retain=retain)

    def clear_speaker_retained(self, mac6: str) -> None:
        """Clear a removed speaker's retained status and state topics.

        Otherwise the broker replays them on the next reconnect and the
        speaker reappears in discovery, so the clears are queued while the
        link is down. Raises AudioAdapterNotReady when the adapter is stopped.
        """
        client = self._running_client()
        for topic in (speaker_status_topic(mac6), speaker_state_topic(mac6)):
            client.publish(topic, "", qos=1, retain=True)

    async def async_discover(self) -> list[dict[str, Any]]:
        """Ping the speakers and return the un-enrolled ones heard so far.

        Doesn't wait for replies: retained messages filled the engine on
        connect, and the ping only refreshes announces.
        """
        if self._started and self._client is not None:
            self._client.publish(_TOPIC_PING, "", qos=0)
        return self._engine.discovered()
