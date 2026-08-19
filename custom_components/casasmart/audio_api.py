"""CasaSmart runtime component."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .audio import (
    AudioEngine,
    AudioError,
    UnknownSpeakerError,
    CMD_RESET,
    TOPIC_ATHAN_CONFIG,
    normalize_mac6,
)
from .audio_adapter import AudioAdapter, AudioAdapterNotReady
from .auth_api import (
    authenticate_request,
    get_extra_lan_cidrs,
    get_provision_secret,
    is_lan_request,
    json_body,
)
from .const import DOMAIN, EVENT_AUDIO_CHANGED

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)



_PA_MAX_BYTES = 16 * 1024 * 1024



_PA_CLIP_TTL = 120.0

_PA_CLIP_MAX_COUNT = 16
_PA_STORE_KEY = f"{DOMAIN}_pa_clips"


class PaClipStore:
    """CasaSmart runtime component."""

    def __init__(
        self, ttl: float = _PA_CLIP_TTL, max_count: int = _PA_CLIP_MAX_COUNT
    ) -> None:
        self._ttl = ttl
        self._max_count = max_count

        self._clips: dict[str, tuple[bytes, str, float]] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_d, _c, exp) in self._clips.items() if exp <= now]:
            del self._clips[token]

    def put(self, data: bytes, content_type: str) -> str:
        """CasaSmart runtime component."""
        self._evict_expired()
        while len(self._clips) >= self._max_count:
            oldest = min(self._clips, key=lambda t: self._clips[t][2])
            del self._clips[oldest]
        token = secrets.token_urlsafe(24)
        self._clips[token] = (data, content_type, time.monotonic() + self._ttl)
        return token

    def get(self, token: str) -> tuple[bytes, str] | None:
        """CasaSmart runtime component."""
        self._evict_expired()
        item = self._clips.get(token)
        if item is None:
            return None
        return item[0], item[1]


def _pa_store(hass: HomeAssistant) -> PaClipStore:
    """CasaSmart runtime component."""
    store = hass.data.get(_PA_STORE_KEY)
    if store is None:
        store = PaClipStore()
        hass.data[_PA_STORE_KEY] = store
    return store


def get_audio(hass: HomeAssistant) -> AudioEngine | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.audio


def get_audio_adapter(hass: HomeAssistant) -> AudioAdapter | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.audio_adapter


def get_athan_scheduler(hass: HomeAssistant):
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.athan_scheduler


class _AudioView(HomeAssistantView):
    """CasaSmart runtime component."""

    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _audio_or_503(self) -> tuple[AudioEngine | None, web.Response | None]:
        audio = get_audio(self._hass)
        if audio is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return audio, None

    def _adapter_or_503(self) -> tuple[AudioAdapter | None, web.Response | None]:
        adapter = get_audio_adapter(self._hass)
        if adapter is None:
            return None, self.json_message(
                "Audio bus not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return adapter, None

    def _notify_change(self) -> None:
        """CasaSmart runtime component."""
        self._hass.bus.async_fire(EVENT_AUDIO_CHANGED, None)

    def _publish_or_503(
        self, adapter: AudioAdapter, topic: str, payload: Any, *, retain: bool = False
    ) -> web.Response | None:
        """CasaSmart runtime component."""
        try:
            adapter.publish(topic, payload, qos=1, retain=retain)
        except AudioAdapterNotReady as err:
            return self.json_message(str(err), HTTPStatus.SERVICE_UNAVAILABLE)
        return None





def _scoped_area_names(hass: HomeAssistant, scope: list[str]) -> set[str]:
    """CasaSmart runtime component."""
    registry = ar.async_get(hass)
    names: set[str] = set()
    for area_id in scope:
        area = registry.async_get_area(area_id)
        if area is not None and area.name:
            names.add(area.name.strip().casefold())
    return names


def _speaker_in_scope(
    speaker: dict[str, Any],
    allowed_ids: set[str],
    allowed_names: set[str],
) -> bool:
    """CasaSmart runtime component."""
    area_id = speaker.get("area_id")
    if area_id:
        return area_id in allowed_ids
    room = speaker.get("room")
    if room:
        return room.strip().casefold() in allowed_names
    return True


class CasaSmartAudioSpeakersView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/speakers"
    name = f"api:{DOMAIN}:audio:speakers"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.read")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready

        speakers = audio.speakers()
        scope = claims.get("rooms")
        if scope is not None:








            allowed_ids = set(scope)
            allowed_names = _scoped_area_names(self._hass, scope)
            speakers = [s for s in speakers if _speaker_in_scope(s, allowed_ids, allowed_names)]
        return self.json({"speakers": speakers})

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            record = await self._hass.async_add_executor_job(
                _enroll_job,
                audio,
                payload.get("mac"),
                payload.get("name"),
                payload.get("room"),
                payload.get("icon"),
                payload.get("room_id"),
            )
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._notify_change()
        return self.json({"speaker": record})


class CasaSmartAudioSpeakerView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/speakers/{{mac6}}"
    name = f"api:{DOMAIN}:audio:speaker"

    async def put(self, request: web.Request, mac6: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            record = await self._hass.async_add_executor_job(
                _update_job,
                audio,
                mac6,
                payload.get("name"),
                payload.get("room"),
                payload.get("icon"),
                payload.get("room_id"),
            )
        except UnknownSpeakerError as err:
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._notify_change()
        return self.json({"speaker": record})

    async def delete(self, request: web.Request, mac6: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready





        try:
            reset_topic, reset_msg = audio.build_command(mac6, CMD_RESET)
            norm_mac6 = normalize_mac6(mac6)
            await self._hass.async_add_executor_job(audio.remove_speaker, mac6)
        except UnknownSpeakerError as err:
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._deprovision_speaker(norm_mac6, reset_topic, reset_msg)
        self._notify_change()
        return self.json({"deleted": norm_mac6})

    def _deprovision_speaker(
        self, mac6: str, reset_topic: str, reset_msg: Any
    ) -> None:
        """CasaSmart runtime component."""
        adapter = get_audio_adapter(self._hass)
        if adapter is None:
            return
        try:
            adapter.publish(reset_topic, reset_msg, qos=1)
            adapter.clear_speaker_retained(mac6)
        except AudioAdapterNotReady:
            _LOGGER.info(
                "Speaker %s removed from registry but bus is down — reset/retain"
                " clear skipped (it may briefly reappear as a ghost until it is"
                " power-cycled)",
                mac6,
            )


class CasaSmartAudioDiscoverView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/discover"
    name = f"api:{DOMAIN}:audio:discover"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        adapter, not_ready = self._adapter_or_503()
        if not_ready is not None:
            return not_ready



        discovered = await adapter.async_discover()
        return self.json({"discovered": discovered})





class CasaSmartAudioCommandView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/speakers/{{mac6}}/command"
    name = f"api:{DOMAIN}:audio:command"

    async def post(self, request: web.Request, mac6: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.control")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        adapter, adapter_not_ready = self._adapter_or_503()
        if adapter_not_ready is not None:
            return adapter_not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            topic, message = audio.build_command(
                mac6, payload.get("cmd"), value=payload.get("value")
            )
        except UnknownSpeakerError as err:
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        published_error = self._publish_or_503(adapter, topic, message)
        if published_error is not None:
            return published_error
        return self.json({"ok": True, "topic": topic, "command": message})


class CasaSmartAudioAirplayView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/speakers/{{mac6}}/airplay"
    name = f"api:{DOMAIN}:audio:airplay"

    async def post(self, request: web.Request, mac6: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.control")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        adapter, adapter_not_ready = self._adapter_or_503()
        if adapter_not_ready is not None:
            return adapter_not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            topic, verb = audio.build_airplay_remote(mac6, payload.get("action"))
        except UnknownSpeakerError as err:
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        published_error = self._publish_or_503(adapter, topic, verb)
        if published_error is not None:
            return published_error
        return self.json({"ok": True, "topic": topic, "action": verb})


class CasaSmartAudioBroadcastView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/broadcast"
    name = f"api:{DOMAIN}:audio:broadcast"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.control")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        adapter, adapter_not_ready = self._adapter_or_503()
        if adapter_not_ready is not None:
            return adapter_not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            topic, message = audio.build_play(
                url=payload.get("url"),
                file=payload.get("file"),
                volume=payload.get("volume"),
                priority=payload.get("priority"),
            )
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        published_error = self._publish_or_503(adapter, topic, message)
        if published_error is not None:
            return published_error
        return self.json({"ok": True, "topic": topic, "command": message})


class CasaSmartAudioPaView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/pa"
    name = f"api:{DOMAIN}:audio:pa"

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.control")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        adapter, adapter_not_ready = self._adapter_or_503()
        if adapter_not_ready is not None:
            return adapter_not_ready

        _filename, content_type, data, targets, read_error = await self._read_pa_parts(
            request
        )
        if read_error is not None:
            return read_error




        token = _pa_store(self._hass).put(
            data, content_type or "application/octet-stream"
        )
        clip_path = f"/api/{DOMAIN}/audio/pa-clip/{token}"



        played_on: list[str] = []
        try:
            if targets:
                for mac6 in targets:
                    try:
                        topic, message = audio.build_play(
                            mac=mac6, url=clip_path, priority="pa"
                        )
                    except UnknownSpeakerError:
                        _LOGGER.warning("PA target %s not enrolled — skipped", mac6)
                        continue
                    published_error = self._publish_or_503(adapter, topic, message)
                    if published_error is not None:
                        return published_error
                    played_on.append(mac6)
                if not played_on:
                    return self.json_message(
                        "None of the target speakers are enrolled",
                        HTTPStatus.NOT_FOUND,
                    )
            else:
                topic, message = audio.build_play(url=clip_path, priority="pa")
                published_error = self._publish_or_503(adapter, topic, message)
                if published_error is not None:
                    return published_error
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        return self.json(
            {
                "ok": True,
                "played_on": played_on or "all",
                "clip_ttl": _pa_store(self._hass).ttl,
            }
        )

    async def _read_pa_parts(
        self, request: web.Request
    ) -> tuple[str, str, bytes, list[str], web.Response | None]:
        """CasaSmart runtime component."""
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError):
            return "", "", b"", [], self.json_message(
                "Body must be multipart/form-data with an 'audio' file",
                HTTPStatus.BAD_REQUEST,
            )
        filename = content_type = ""
        data: bytes | None = None
        targets: list[str] = []
        async for part in reader:
            if part.name == "targets":
                targets = _parse_targets(await part.text())
                continue
            if part.name != "audio":
                continue
            filename = part.filename or "pa.mp3"
            content_type = part.headers.get("Content-Type", "application/octet-stream")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > _PA_MAX_BYTES:
                    return "", "", b"", [], self.json_message(
                        f"Audio too large (max {_PA_MAX_BYTES} bytes)",
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
        if data is None:
            return "", "", b"", [], self.json_message(
                "Missing 'audio' file part", HTTPStatus.BAD_REQUEST
            )
        return filename, content_type, data, targets, None


class CasaSmartAudioPaClipView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/pa-clip/{{token}}"
    name = f"api:{DOMAIN}:audio:pa-clip"

    async def get(self, request: web.Request, token: str) -> web.Response:
        item = _pa_store(self._hass).get(token)
        if item is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        data, content_type = item
        return web.Response(body=data, content_type=content_type)





class CasaSmartAudioAthanView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/athan"
    name = f"api:{DOMAIN}:audio:athan"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.read")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        athan = audio.get_athan()




        cfg = self._hass.config
        pinned = athan.get("lat") is not None and athan.get("lon") is not None
        location = {
            "lat": athan.get("lat") if pinned else cfg.latitude,
            "lon": athan.get("lon") if pinned else cfg.longitude,
            "timezone": athan.get("timezone") or cfg.time_zone,
            "source": "config" if pinned else "home",
        }



        scheduler = get_athan_scheduler(self._hass)
        schedule = scheduler.schedule_snapshot() if scheduler is not None else None
        return self.json({"athan": athan, "location": location, "schedule": schedule})

    async def put(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        config = payload.get("athan", payload)
        try:
            stored = await self._hass.async_add_executor_job(audio.set_athan, config)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)



        adapter = get_audio_adapter(self._hass)
        relayed = False
        if adapter is not None:
            try:
                adapter.publish(TOPIC_ATHAN_CONFIG, stored, qos=1, retain=True)
                relayed = True
            except AudioAdapterNotReady:
                _LOGGER.warning(
                    "Athan config stored but not relayed — MQTT bus is down"
                )



        scheduler = get_athan_scheduler(self._hass)
        if scheduler is not None:
            scheduler.reschedule()
        return self.json({"athan": stored, "relayed": relayed})





class CasaSmartAudioBrokerView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/broker"
    name = f"api:{DOMAIN}:audio:broker"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        return self.json({"broker": _redact_secret(audio.get_broker(), "password")})

    async def put(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            broker = await self._hass.async_add_executor_job(
                _set_broker_job, audio, payload
            )
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)

        adapter = get_audio_adapter(self._hass)
        if adapter is not None:
            await adapter.async_reconfigure()
        return self.json({"broker": _redact_secret(broker, "password")})


class CasaSmartAudioPaConfigView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/pa-config"
    name = f"api:{DOMAIN}:audio:pa-config"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        return self.json({"pa": _redact_secret(audio.get_pa(), "api_key")})

    async def put(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.manage")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            pa = await self._hass.async_add_executor_job(_set_pa_job, audio, payload)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json({"pa": _redact_secret(pa, "api_key")})





class CasaSmartAudioProvisionView(_AudioView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/audio/provision"
    name = f"api:{DOMAIN}:audio:provision"

    async def get(self, request: web.Request) -> web.Response:
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready





        secret = get_provision_secret(self._hass)
        presented = request.headers.get("X-CasaSmart-Provision-Key", "")
        secret_ok = bool(secret) and hmac.compare_digest(presented, secret)
        if not secret_ok and not is_lan_request(
            request, get_extra_lan_cidrs(self._hass)
        ):
            _LOGGER.warning(
                "Audio provision refused (bad/absent key, non-LAN source: %s)",
                request.remote,
            )
            return self.json_message(
                "Provisioning requires the hub's provisioning key or LAN access",
                HTTPStatus.FORBIDDEN,
            )
        broker = audio.provision()
        if not broker.get("host"):
            return self.json_message(
                "Broker not provisioned on the hub yet",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return self.json({"broker": broker})





def _parse_targets(raw: Any) -> list[str]:
    """CasaSmart runtime component."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    items: list[Any]
    text = raw.strip()
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = []
        items = parsed if isinstance(parsed, list) else []
    else:
        items = re.split(r"[,\s]+", text)
    result: list[str] = []
    for item in items:
        try:
            mac6 = normalize_mac6(item)
        except AudioError:
            continue
        if mac6 not in result:
            result.append(mac6)
    return result


def _redact_secret(config: dict[str, Any], field: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    redacted = dict(config)
    redacted[f"{field}_set"] = bool(redacted.pop(field, None))
    return redacted







def _enroll_job(audio: AudioEngine, mac, name, room, icon, area_id):
    return audio.enroll_speaker(mac, name, room, icon=icon, area_id=area_id)


def _update_job(audio: AudioEngine, mac6, name, room, icon, area_id):
    return audio.update_speaker(
        mac6, name=name, room=room, icon=icon, area_id=area_id
    )


def _set_broker_job(audio: AudioEngine, payload: dict[str, Any]):
    return audio.set_broker(
        host=payload.get("host"),
        port=payload.get("port"),
        tls=payload.get("tls"),
        username=payload.get("username"),
        password=payload.get("password"),
    )


def _set_pa_job(audio: AudioEngine, payload: dict[str, Any]):
    return audio.set_pa(
        host=payload.get("host"),
        port=payload.get("port"),
        api_key=payload.get("api_key"),
    )
