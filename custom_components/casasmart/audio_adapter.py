"""CasaSmart runtime component."""

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
    """CasaSmart runtime component."""




_CLIENT_ID = "casasmart-hub"


_TOPIC_ANNOUNCE = "speakers/announce"
_TOPIC_PING = "speakers/ping"
_SUB_STATUS = "speakers/+/status"
_SUB_STATE = "speakers/+/state"

_TOPIC_RE = re.compile(r"^speakers/([0-9a-fA-F]+)/(status|state)$")


_RECONNECT_MIN_DELAY = 1
_RECONNECT_MAX_DELAY = 60


def _build_paho_client(client_id: str) -> Any:
    """CasaSmart runtime component."""
    import paho.mqtt.client as mqtt

    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
            clean_session=True,
        )
    return mqtt.Client(client_id=client_id, clean_session=True)


class AudioAdapter:
    """CasaSmart runtime component."""

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


        self._started = False



    async def async_start(self) -> None:
        """CasaSmart runtime component."""
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
        except Exception:
            _LOGGER.exception(
                "CasaSmart audio: failed to start MQTT to %s:%s", host, port
            )
            self._client = None
            self._started = False
            return
        _LOGGER.info("CasaSmart audio: MQTT client connecting to %s:%s", host, port)

    async def async_stop(self) -> None:
        """CasaSmart runtime component."""
        client = self._client
        self._client = None
        self._started = False
        if client is None:
            return

        await self._hass.async_add_executor_job(self._teardown_client, client)

    @staticmethod
    def _teardown_client(client: Any) -> None:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            _LOGGER.exception("CasaSmart audio: error stopping MQTT client")

    async def async_reconfigure(self) -> None:
        """CasaSmart runtime component."""
        await self.async_stop()
        await self.async_start()



    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, rc: Any) -> None:
        """CasaSmart runtime component."""
        if rc != 0:
            _LOGGER.warning("CasaSmart audio: MQTT connect failed (rc=%s)", rc)
            return
        client.subscribe(
            [(_TOPIC_ANNOUNCE, 0), (_SUB_STATUS, 1), (_SUB_STATE, 0)]
        )
        client.publish(_TOPIC_PING, "", qos=0)





        self._republish_athan(client)
        _LOGGER.info("CasaSmart audio: MQTT connected, subscribed to speaker topics")

    def _republish_athan(self, client: Any) -> None:
        """CasaSmart runtime component."""
        try:
            athan = self._engine.get_athan()
            if athan:
                client.publish(
                    TOPIC_ATHAN_CONFIG, json.dumps(athan), qos=1, retain=True
                )
        except Exception:
            _LOGGER.exception("CasaSmart audio: failed to re-publish athan config")

    def _on_disconnect(self, _client: Any, _userdata: Any, rc: Any) -> None:


        if rc not in (0, None):
            _LOGGER.warning("CasaSmart audio: MQTT dropped (rc=%s), reconnecting", rc)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """CasaSmart runtime component."""
        try:
            changed = self._ingest(message.topic, message.payload)
        except Exception:
            _LOGGER.exception(
                "CasaSmart audio: failed to ingest %s", getattr(message, "topic", "?")
            )
            return
        if changed:
            self._nudge_changed()

    def _ingest(self, topic: str, payload: Any) -> bool:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        self._hass.loop.call_soon_threadsafe(self._fire_changed)



    def publish(
        self, topic: str, payload: Any, *, qos: int = 1, retain: bool = False
    ) -> None:
        """CasaSmart runtime component."""
        if not self._started or self._client is None:
            raise AudioAdapterNotReady("Audio MQTT client is not connected")
        body = json.dumps(payload) if not isinstance(payload, (str, bytes)) else payload
        self._client.publish(topic, body, qos=qos, retain=retain)

    def clear_speaker_retained(self, mac6: str) -> None:
        """CasaSmart runtime component."""
        if not self._started or self._client is None:
            raise AudioAdapterNotReady("Audio MQTT client is not connected")
        for topic in (speaker_status_topic(mac6), speaker_state_topic(mac6)):
            self._client.publish(topic, "", qos=1, retain=True)

    async def async_discover(self) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        if self._started and self._client is not None:
            self._client.publish(_TOPIC_PING, "", qos=0)
        return self._engine.discovered()
