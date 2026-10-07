"""Audio REST endpoints over AudioEngine and AudioAdapter.

The app never holds broker credentials or talks MQTT itself; it reads hub
state and sends commands here. Endpoints under /api/casasmart, with the
permission each needs:

- GET     /audio/speakers                 speakers + live status (audio.read)
- GET     /audio/discover                 un-enrolled speakers (audio.manage)
- POST    /audio/speakers                 enroll a speaker (audio.manage)
- PUT     /audio/speakers/{mac6}          rename, move or re-icon (audio.manage)
- DELETE  /audio/speakers/{mac6}          remove and reset (audio.manage)
- POST    /audio/speakers/{mac6}/command  volume, stop, pause... (audio.control)
- POST    /audio/speakers/{mac6}/airplay  AirPlay transport (audio.control)
- POST    /audio/broadcast                play on every speaker (audio.control)
- POST    /audio/pa                       upload and play a PA clip (audio.control)
- GET/PUT /audio/athan                    athan config (audio.read / audio.manage)
- GET/PUT /audio/broker                   broker settings (audio.manage)
- GET/PUT /audio/pa-config                PA service settings (audio.manage)
- GET     /audio/provision                broker login for speakers (provisioning key)
- GET     /audio/pa-clip/{token}          a hosted PA clip (the token is the credential)

A room-scoped caller sees and controls only the speakers in its rooms plus
the unassigned ones. Speaker changes fire EVENT_AUDIO_CHANGED.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.http_exceptions import BadHttpMessage
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .audio import (
    CMD_RESET,
    TOPIC_ATHAN_CONFIG,
    AudioEngine,
    AudioError,
    UnknownSpeakerError,
    normalize_mac6,
    speaker_command_topic,
)
from .audio_adapter import AudioAdapter, AudioAdapterNotReady
from .auth_api import (
    authenticate_request,
    get_provision_secret,
    is_keyless_speaker_provisioning_enabled,
    is_lan_request,
    json_body,
)
from .const import DOMAIN, EVENT_AUDIO_CHANGED

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)

# Largest PA upload accepted; clips are short voice recordings.
_PA_MAX_BYTES = 16 * 1024 * 1024
# Seconds a hosted clip stays fetchable. Speakers fetch it within about a
# second of the play command.
_PA_CLIP_TTL = 120.0
# Clips hosted at once, against upload floods.
_PA_CLIP_MAX_COUNT = 16
_PA_STORE_KEY = f"{DOMAIN}_pa_clips"
# A room-scoped caller played to every speaker but has none in its rooms.
_NO_SPEAKERS_IN_SCOPE = "No speakers in your rooms"


# -- runtime accessors --------------------------------------------------------


def get_audio(hass: HomeAssistant) -> AudioEngine | None:
    """The loaded entry's audio engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.audio


def get_audio_adapter(hass: HomeAssistant) -> AudioAdapter | None:
    """The loaded entry's audio MQTT adapter (None until/unless started)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.audio_adapter


def get_athan_scheduler(hass: HomeAssistant):
    """The loaded entry's hub-native athan scheduler (None until started)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.athan_scheduler


# -- room scope ---------------------------------------------------------------


def _scoped_area_names(hass: HomeAssistant, scope: list[str]) -> set[str]:
    """Casefolded names of the HA areas in a token's room scope."""
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
    """Whether a room-scoped caller may see this speaker.

    A speaker with an area_id must be in scope. Without one, its free-text
    room must match the name of a scoped area. A speaker with neither is
    shared and visible to everyone.
    """
    area_id = speaker.get("area_id")
    if area_id:
        return area_id in allowed_ids
    room = speaker.get("room")
    if room:
        return room.strip().casefold() in allowed_names
    return True


def _controllable_speakers(
    hass: HomeAssistant, audio: AudioEngine, claims: dict[str, Any]
) -> set[str] | None:
    """The mac6s a caller may control, or None if it isn't room-scoped.

    The same set GET /audio/speakers shows it, so a caller can't drive a
    speaker it can't see.
    """
    scope = claims.get("rooms")
    if scope is None:
        return None
    allowed_ids = set(scope)
    allowed_names = _scoped_area_names(hass, scope)
    return {
        speaker["mac6"]
        for speaker in audio.speakers()
        if _speaker_in_scope(speaker, allowed_ids, allowed_names)
    }


def _require_controllable(mac: Any, allowed: set[str] | None) -> None:
    """Treat a speaker outside the caller's scope as not enrolled.

    The 404 matches an unknown id's, so a room-scoped caller can't learn which
    speakers exist elsewhere. A malformed id still raises AudioError.
    """
    if allowed is None:
        return
    mac6 = normalize_mac6(mac)
    if mac6 not in allowed:
        raise UnknownSpeakerError(f"No speaker enrolled under {mac6!r}")


# -- helpers ------------------------------------------------------------------


def _parse_targets(raw: Any) -> list[str]:
    """Parse a PA targets field into unique mac6s, keeping their order.

    Accepts a JSON array or a comma- or space-separated string. Invalid ids
    are dropped so a bad selection never blocks the announcement.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    items: list[Any]
    text = raw.strip()
    if text.startswith("["):
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
    """Copy config with field replaced by a boolean <field>_set.

    The app only needs to know whether a password or key is set. The broker
    password leaves the hub only through the provision endpoint.
    """
    redacted = dict(config)
    redacted[f"{field}_set"] = bool(redacted.pop(field, None))
    return redacted


# -- executor jobs (storage-touching engine calls) ----------------------------
# async_add_executor_job passes positional arguments only; these map them to
# the engine's keyword arguments.


def _enroll_job(audio: AudioEngine, mac, name, room, icon, area_id):
    return audio.enroll_speaker(mac, name, room, icon=icon, area_id=area_id)


def _update_job(audio: AudioEngine, mac6, name, room, icon, area_id):
    return audio.update_speaker(mac6, name=name, room=room, icon=icon, area_id=area_id)


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


# -- PA clip hosting ----------------------------------------------------------


class PaClipStore:
    """In-memory PA clips the hub hosts for its speakers.

    Each clip is stored under a random token for a short time, like a
    presigned URL: the token is the credential, so the fetch endpoint needs no
    JWT or LAN check (a LAN check would reject speakers behind Docker's NAT).
    Used only on the event loop, so there is no locking; expired clips are
    evicted on access.
    """

    def __init__(
        self, ttl: float = _PA_CLIP_TTL, max_count: int = _PA_CLIP_MAX_COUNT
    ) -> None:
        self._ttl = ttl
        self._max_count = max_count
        # token -> (data, content_type, expires_at_monotonic)
        self._clips: dict[str, tuple[bytes, str, float]] = {}

    @property
    def ttl(self) -> float:
        """Seconds a stored clip stays fetchable."""
        return self._ttl

    def _evict_expired(self) -> None:
        """Drop every clip whose TTL has passed."""
        now = time.monotonic()
        for token in [t for t, (_d, _c, exp) in self._clips.items() if exp <= now]:
            del self._clips[token]

    def put(self, data: bytes, content_type: str) -> str:
        """Store a clip and return its token; drops the oldest if at capacity."""
        self._evict_expired()
        while len(self._clips) >= self._max_count:
            oldest = min(self._clips, key=lambda t: self._clips[t][2])
            del self._clips[oldest]
        token = secrets.token_urlsafe(24)  # 192 bits
        self._clips[token] = (data, content_type, time.monotonic() + self._ttl)
        return token

    def get(self, token: str) -> tuple[bytes, str] | None:
        """Return (data, content_type) for a live token, else None."""
        self._evict_expired()
        item = self._clips.get(token)
        if item is None:
            return None
        return item[0], item[1]


def _pa_store(hass: HomeAssistant) -> PaClipStore:
    """The per-hass PA clip store, created on first use."""
    store = hass.data.get(_PA_STORE_KEY)
    if store is None:
        store = PaClipStore()
        hass.data[_PA_STORE_KEY] = store
    return store


# -- views --------------------------------------------------------------------


class _AudioView(HomeAssistantView):
    """Shared plumbing for the audio views."""

    requires_auth = False  # each handler calls authenticate_request

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _audio_or_503(self) -> tuple[AudioEngine | None, web.Response | None]:
        """(engine, None), or (None, 503) while the hub is loading."""
        audio = get_audio(self._hass)
        if audio is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return audio, None

    def _adapter_or_503(self) -> tuple[AudioAdapter | None, web.Response | None]:
        """(adapter, None), or (None, 503) before the adapter exists."""
        adapter = get_audio_adapter(self._hass)
        if adapter is None:
            return None, self.json_message(
                "Audio bus not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return adapter, None

    def _notify_change(self) -> None:
        """Tell connected apps the speakers changed."""
        self._hass.bus.async_fire(EVENT_AUDIO_CHANGED, None)

    def _publish_or_503(
        self, adapter: AudioAdapter, topic: str, payload: Any, *, retain: bool = False
    ) -> web.Response | None:
        """Publish through the adapter; a bus that is down is a 503."""
        try:
            adapter.publish(topic, payload, qos=1, retain=retain)
        except AudioAdapterNotReady as err:
            return self.json_message(str(err), HTTPStatus.SERVICE_UNAVAILABLE)
        return None


# -- speaker registry + live status -------------------------------------------


class CasaSmartAudioSpeakersView(_AudioView):
    """GET /audio/speakers lists enrolled speakers; POST enrolls one."""

    url = f"/api/{DOMAIN}/audio/speakers"
    name = f"api:{DOMAIN}:audio:speakers"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.read")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        # In memory, so no executor hop.
        speakers = audio.speakers()
        scope = claims.get("rooms")
        if scope is not None:
            allowed_ids = set(scope)
            allowed_names = _scoped_area_names(self._hass, scope)
            speakers = [
                s for s in speakers if _speaker_in_scope(s, allowed_ids, allowed_names)
            ]
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
    """PUT/DELETE /audio/speakers/{mac6}: update or remove one speaker."""

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
        # Build the reset command first: it needs an enrolled speaker, so it
        # is also the 404 check.
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

    def _deprovision_speaker(self, mac6: str, reset_topic: str, reset_msg: Any) -> None:
        """Best effort: reset the speaker and clear its retained topics.

        Otherwise the broker replays them and the speaker shows up in
        discovery again.
        """
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
    """GET /audio/discover: un-enrolled speakers, for the add-speaker list.

    Needs audio.manage because it is part of setup.
    """

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


# -- control ------------------------------------------------------------------


class CasaSmartAudioCommandView(_AudioView):
    """POST /audio/speakers/{mac6}/command: one speaker control.

    Body: {"cmd": "volume", "value": 40}, {"cmd": "stop"} and so on.
    """

    url = f"/api/{DOMAIN}/audio/speakers/{{mac6}}/command"
    name = f"api:{DOMAIN}:audio:command"

    async def post(self, request: web.Request, mac6: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.control")
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
            _require_controllable(
                mac6, _controllable_speakers(self._hass, audio, claims)
            )
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
    """POST /audio/speakers/{mac6}/airplay: AirPlay transport control.

    Body: {"action": "playpause"} (or play, pause, next, previous, stop).
    shairport-sync passes it on to the device that is AirPlaying, so the
    phone's own playback responds. Nothing happens if no one is AirPlaying.
    """

    url = f"/api/{DOMAIN}/audio/speakers/{{mac6}}/airplay"
    name = f"api:{DOMAIN}:audio:airplay"

    async def post(self, request: web.Request, mac6: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.control")
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
            _require_controllable(
                mac6, _controllable_speakers(self._hass, audio, claims)
            )
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
    """POST /audio/broadcast: play a URL or file on every speaker.

    Body: url or file (one of them), plus optional volume and priority. Audio
    uploads go to POST /audio/pa. For a room-scoped caller the play is sent to
    each speaker it can see instead of the broadcast topic.
    """

    url = f"/api/{DOMAIN}/audio/broadcast"
    name = f"api:{DOMAIN}:audio:broadcast"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.control")
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
        allowed = _controllable_speakers(self._hass, audio, claims)
        if allowed is not None:
            if not allowed:
                return self.json_message(_NO_SPEAKERS_IN_SCOPE, HTTPStatus.NOT_FOUND)
            played_on = sorted(allowed)
            for mac6 in played_on:
                published_error = self._publish_or_503(
                    adapter, speaker_command_topic(mac6), message
                )
                if published_error is not None:
                    return published_error
            return self.json({"ok": True, "played_on": played_on, "command": message})
        published_error = self._publish_or_503(adapter, topic, message)
        if published_error is not None:
            return published_error
        return self.json({"ok": True, "topic": topic, "command": message})


class CasaSmartAudioPaView(_AudioView):
    """POST /audio/pa: host an uploaded PA clip and play it.

    The app sends multipart/form-data with an audio file and optional targets.
    The hub hosts the clip under a random token (CasaSmartAudioPaClipView) and
    sends a play command with its hub-relative path. Each speaker resolves the
    path against the hub address it is connected to, so the clip is fetched
    over the LAN even when the app uploaded it through the tunnel.

    A room-scoped caller plays only on speakers it can see: no targets means
    all of those, and a target outside them refuses the upload with a 404.
    """

    url = f"/api/{DOMAIN}/audio/pa"
    name = f"api:{DOMAIN}:audio:pa"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.control")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        adapter, adapter_not_ready = self._adapter_or_503()
        if adapter_not_ready is not None:
            return adapter_not_ready

        parts = await self._read_pa_parts(request)
        if isinstance(parts, web.Response):
            return parts
        content_type, data, targets = parts

        allowed = _controllable_speakers(self._hass, audio, claims)
        if allowed is not None:
            # Checked before anything is hosted or played. Unknown targets are
            # refused too: skipping them would reveal which ids exist.
            try:
                for mac6 in targets:
                    _require_controllable(mac6, allowed)
            except UnknownSpeakerError as err:
                return self.json_message(str(err), HTTPStatus.NOT_FOUND)
            if not targets:
                if not allowed:
                    return self.json_message(
                        _NO_SPEAKERS_IN_SCOPE, HTTPStatus.NOT_FOUND
                    )
                targets = sorted(allowed)

        token = _pa_store(self._hass).put(
            data, content_type or "application/octet-stream"
        )
        clip_path = f"/api/{DOMAIN}/audio/pa-clip/{token}"

        # An un-enrolled target is skipped rather than failing the announcement.
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
    ) -> tuple[str, bytes, list[str]] | web.Response:
        """Read the audio part (size-capped) and the optional targets part.

        Returns (content_type, data, targets), or an error response. No
        targets means every speaker.
        """
        not_multipart = "Body must be multipart/form-data with an 'audio' file"
        try:
            reader = await request.multipart()
        except (AssertionError, KeyError, ValueError):
            return self.json_message(not_multipart, HTTPStatus.BAD_REQUEST)
        content_type = ""
        data: bytes | None = None
        targets: list[str] = []
        try:
            async for part in reader:
                # A nested multipart part has no name; it is not an upload field.
                name = getattr(part, "name", None)
                if name == "targets":
                    targets = _parse_targets(await part.text())
                    continue
                if name != "audio":
                    continue
                content_type = part.headers.get(
                    "Content-Type", "application/octet-stream"
                )
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _PA_MAX_BYTES:
                        return self.json_message(
                            f"Audio too large (max {_PA_MAX_BYTES} bytes)",
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
        except (AssertionError, BadHttpMessage, LookupError, ValueError):
            # What aiohttp's multipart parser raises for a malformed body (some
            # versions assert), and text() for a field it can't decode.
            return self.json_message(not_multipart, HTTPStatus.BAD_REQUEST)
        if data is None:
            return self.json_message(
                "Missing 'audio' file part", HTTPStatus.BAD_REQUEST
            )
        return content_type, data, targets


class CasaSmartAudioPaClipView(_AudioView):
    """GET /audio/pa-clip/{token}: serve a hosted PA clip to the speakers.

    The unguessable, short-lived token is the only check. A LAN check would
    reject the speakers behind Docker, where every source looks non-LAN.
    Served over plain HTTP so the speaker's wget needs no TLS.
    """

    url = f"/api/{DOMAIN}/audio/pa-clip/{{token}}"
    name = f"api:{DOMAIN}:audio:pa-clip"

    async def get(self, request: web.Request, token: str) -> web.Response:
        item = _pa_store(self._hass).get(token)
        if item is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        data, content_type = item
        return web.Response(body=data, content_type=content_type)


# -- athan config -------------------------------------------------------------


class CasaSmartAudioAthanView(_AudioView):
    """GET/PUT /audio/athan: the athan config.

    GET (audio.read) returns the config, the location in effect and the
    current schedule. PUT (audio.manage) replaces the config, reschedules and
    publishes it retained to athan/config.
    """

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
        # The location the scheduler uses (see AthanScheduler._resolve_config).
        # source is "home" only when neither coordinate is set.
        cfg = self._hass.config
        lat, lon = athan.get("lat"), athan.get("lon")
        location = {
            "lat": lat if lat is not None else cfg.latitude,
            "lon": lon if lon is not None else cfg.longitude,
            "timezone": athan.get("timezone") or cfg.time_zone,
            "source": "home" if lat is None and lon is None else "config",
        }
        # Lets the app show the next athan, or that scheduling failed.
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
        # A down bus isn't fatal: the adapter republishes the config when it
        # reconnects, and relayed=false tells the app.
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
        # The scheduler reads the engine rather than the bus.
        scheduler = get_athan_scheduler(self._hass)
        if scheduler is not None:
            await scheduler.async_reschedule()
        return self.json({"athan": stored, "relayed": relayed})


# -- installer: broker / PA credentials ---------------------------------------


class CasaSmartAudioBrokerView(_AudioView):
    """GET/PUT /audio/broker: MQTT broker settings (audio.manage).

    PUT takes any of host, port, tls, username and password, then reconnects
    the hub's MQTT client. Responses carry password_set instead of the
    password.
    """

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
    """GET/PUT /audio/pa-config: PA service settings (audio.manage).

    Responses carry api_key_set instead of the key.
    """

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


# -- speaker provisioning -----------------------------------------------------


class CasaSmartAudioProvisionView(_AudioView):
    """GET /audio/provision: the broker login for the speaker agent.

    The speaker sends the hub's provision_secret in X-CasaSmart-Provision-Key.
    The key works from any source, so a hub behind Docker's NAT still
    provisions. Keyless access from the LAN is opt-in (hub_config
    keyless_speaker_provisioning) because the response holds the broker
    password.
    """

    url = f"/api/{DOMAIN}/audio/provision"
    name = f"api:{DOMAIN}:audio:provision"

    async def get(self, request: web.Request) -> web.Response:
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        secret = get_provision_secret(self._hass)
        presented = request.headers.get("X-CasaSmart-Provision-Key", "")
        # Compared as bytes: compare_digest refuses non-ASCII str, and aiohttp
        # passes non-ASCII header bytes through as text.
        secret_ok = bool(secret) and hmac.compare_digest(
            presented.encode("utf-8", "surrogatepass"),
            secret.encode("utf-8", "surrogatepass"),
        )
        if not secret_ok:
            if not is_keyless_speaker_provisioning_enabled(self._hass):
                _LOGGER.warning(
                    "Audio provision refused (bad/absent key, source: %s)",
                    request.remote,
                )
                return self.json_message(
                    "Provisioning requires the hub's provisioning key",
                    HTTPStatus.FORBIDDEN,
                )
            if not is_lan_request(request):
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
