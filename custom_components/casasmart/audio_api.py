"""CasaSmart audio REST endpoints (Phase 6, block B14) — piece 3.

The app's thin-client surface over the hub-side ``AudioEngine`` +
``AudioAdapter``. This is what flips the speaker stack: the phone stops
holding broker creds and opening its own ``MqttServerClient`` (B14 piece 4
deletes that), and instead reads hub state + fires commands here. The hub is
the only MQTT client (the adapter), the only place the broker/PA creds live
(the engine), and — through ``GET /audio/provision`` — the cred source the Pi
pulls on boot instead of the dead Supabase edge function.

Matches the established API pattern (``alarm_api`` / ``tank_api``): plain views
served on both HA's port and the B10 TLS port, every handler gates in-band with
``authenticate_request``, storage-touching engine calls hop the executor, and
pure in-memory reads (the live mirror) do not.

Endpoints (roles in parens — see ``auth_engine.PERMISSIONS``):

App-facing — speaker registry + live status:
- ``GET    /audio/speakers``               — enrolled speakers + live status (``audio.read``)
- ``GET    /audio/discover``               — un-enrolled speakers seen on the bus (``audio.manage``)
- ``POST   /audio/speakers``               — enroll a discovered speaker (``audio.manage``)
- ``PUT    /audio/speakers/{mac6}``        — rename / re-room (``audio.manage``)
- ``DELETE /audio/speakers/{mac6}``        — drop the speaker (``audio.manage``)

App-facing — control:
- ``POST   /audio/speakers/{mac6}/command``— volume/stop/pause/resume/reset (``audio.control``)
- ``POST   /audio/broadcast``              — play a URL/file to all speakers (``audio.control``)
- ``POST   /audio/pa``                     — proxy a PA audio upload to the PA service (``audio.control``)

App-facing — athan config:
- ``GET    /audio/athan``                  — the stored athan config (``audio.read``)
- ``PUT    /audio/athan``                  — replace it + relay retained (``audio.manage``)

Installer — broker / PA credentials:
- ``GET/PUT /audio/broker``                — broker host/port/tls/user/pass (``audio.manage``)
- ``GET/PUT /audio/pa-config``             — PA service host/port/api-key (``audio.manage``)

Device-facing — the Pi pulls its broker creds on boot:
- ``GET    /audio/provision``              — broker coordinates, provisioning-key or LAN

Mutations that move the hub's view of the speakers (enroll/remove/update) fire
``EVENT_AUDIO_CHANGED`` so the WS server nudges connected apps to re-fetch —
same pattern as ``EVENT_ALARM_CHANGED``. MQTT-driven changes already fire it
from the adapter's ingest path.
"""

from __future__ import annotations

import hmac
import ipaddress
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

# Hard cap on a PA upload so a client can't stream an unbounded body into the
# hub. PA clips are short voice/chime files.
_PA_MAX_BYTES = 16 * 1024 * 1024
# How long a hosted PA clip stays fetchable. The speakers fetch within ~1s of
# the play command, so this is generous; short enough that the unguessable URL
# is a non-issue. Clips are evicted lazily on access — no background sweeper.
_PA_CLIP_TTL = 120.0
# Cap on concurrently-hosted clips (defence against an upload flood).
_PA_CLIP_MAX_COUNT = 16
_PA_STORE_KEY = f"{DOMAIN}_pa_clips"


class PaClipStore:
    """Ephemeral in-memory store of PA clips the hub hosts for its speakers.

    The app uploads a recorded clip; the hub keeps the bytes here under a random
    token and hands the speakers a token URL to fetch (presigned-URL pattern —
    the token + short TTL are the access control, so the fetch endpoint needs no
    JWT, which also side-steps the Docker LAN-gate that would otherwise reject
    the speakers). One event loop → no locking; expired entries are evicted
    lazily on access, so there is no background task.
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
        return self._ttl

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_d, _c, exp) in self._clips.items() if exp <= now]:
            del self._clips[token]

    def put(self, data: bytes, content_type: str) -> str:
        """Store a clip and return its token; drops the oldest if at capacity."""
        self._evict_expired()
        while len(self._clips) >= self._max_count:
            oldest = min(self._clips, key=lambda t: self._clips[t][2])
            del self._clips[oldest]
        token = secrets.token_urlsafe(24)  # ~192 bits of entropy
        self._clips[token] = (data, content_type, time.monotonic() + self._ttl)
        return token

    def get(self, token: str) -> tuple[bytes, str] | None:
        """Return ``(data, content_type)`` for a live token, else None."""
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


class _AudioView(HomeAssistantView):
    """Shared plumbing for the audio views."""

    requires_auth = False  # CasaSmart JWT gate in-handler

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
        """Tell connected apps the hub's speaker view moved."""
        self._hass.bus.async_fire(EVENT_AUDIO_CHANGED, None)

    def _publish_or_503(
        self, adapter: AudioAdapter, topic: str, payload: Any, *, retain: bool = False
    ) -> web.Response | None:
        """Publish through the adapter; map a dead bus to a clean 503."""
        try:
            adapter.publish(topic, payload, qos=1, retain=retain)
        except AudioAdapterNotReady as err:
            return self.json_message(str(err), HTTPStatus.SERVICE_UNAVAILABLE)
        return None


# -- speaker registry + live status -------------------------------------------


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

    Exact ``area_id`` membership first (the modern, robust path); then the
    legacy free-text ``room`` name-match for enrolments made before speakers
    carried an area id; then shared (no room at all) → visible to everyone.
    """
    area_id = speaker.get("area_id")
    if area_id:
        return area_id in allowed_ids
    room = speaker.get("room")
    if room:
        return room.strip().casefold() in allowed_names
    return True


class CasaSmartAudioSpeakersView(_AudioView):
    """GET /audio/speakers — enrolled speakers merged with live status.

    POST /audio/speakers — enroll a (discovered) speaker once it is on the LAN
    and named (the tail of the app's add-speaker flow).
    """

    url = f"/api/{DOMAIN}/audio/speakers"
    name = f"api:{DOMAIN}:audio:speakers"

    async def get(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "audio.read")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        # speakers() copies the in-memory mirror — pure CPU, no executor hop.
        speakers = audio.speakers()
        scope = claims.get("rooms")
        if scope is not None:
            # A room-scoped user (e.g. a guest/kids phone) sees only its rooms'
            # speakers (Phase 8). Preferred path: the speaker's ``area_id`` (==
            # the app's room_id / HA area id) is matched EXACTLY against the
            # caller's room scope — robust, no name-string fuzziness. Legacy
            # fallback: a speaker with no area_id but a free-text ``room`` label
            # is matched by casefolded area NAME (older enrolments). A speaker
            # with neither is shared house infra, visible to all. Fail-closed: a
            # scoped speaker matching nothing is hidden, never leaked.
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
    """PUT/DELETE /audio/speakers/{mac6} — one enrolled speaker."""

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
        # Build the reset command while the speaker is still enrolled — this
        # also doubles as the existence check (404 for an unknown id). Then drop
        # it from the registry and, best-effort, tell the Pi to wipe + re-enter
        # setup and clear its retained topics so it can't resurrect as a
        # discovery ghost on the next reconnect (M3).
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
        """Reset the Pi + clear retained ghosts (best-effort, bus-down safe)."""
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
    """GET /audio/discover — speakers heard on the bus but not yet enrolled.

    Provokes a fresh announce round (the adapter pings the bus) and returns the
    un-enrolled set — the source for the app's add-speaker list. Admin-only
    (it is the install/onboarding surface, not a household action).
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
        # async_discover pings the bus then returns engine.discovered() — the
        # ping is fire-and-forget, the snapshot is the retained truth already in
        # the engine, so this returns immediately.
        discovered = await adapter.async_discover()
        return self.json({"discovered": discovered})


# -- control ------------------------------------------------------------------


class CasaSmartAudioCommandView(_AudioView):
    """POST /audio/speakers/{mac6}/command — a per-speaker control.

    Body: ``{"cmd": "volume", "value": 40}`` or ``{"cmd": "stop"}`` etc. The
    engine validates the command vocabulary + value and builds the exact wire
    payload the Pi agent speaks; the adapter publishes it.
    """

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


class CasaSmartAudioBroadcastView(_AudioView):
    """POST /audio/broadcast — play a URL/file on every speaker.

    Body: ``{"url": "..."}`` or ``{"file": "..."}`` (exactly one), optional
    ``volume`` / ``priority``. For an already-hosted source; uploading raw
    audio goes through ``POST /audio/pa`` instead.
    """

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
    """POST /audio/pa — host a PA clip on the hub and play it on the speakers.

    Hub-native (no external PA service): the app POSTs ``multipart/form-data``
    with an ``audio`` file part (+ optional ``targets``). The hub stores the clip
    under a random token and publishes a ``play`` command carrying the
    hub-relative clip path; each speaker builds a URL from the hub host it is
    already connected to (so it tracks the hub's IP with no extra discovery) and
    fetches the clip over the LAN. The audio never touches the cloud — when the
    app is remote only the upload rides the tunnel; the speakers still fetch
    locally. Served by ``CasaSmartAudioPaClipView``.
    """

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

        filename, content_type, data, targets, read_error = await self._read_pa_parts(
            request
        )
        if read_error is not None:
            return read_error

        # Host the clip under an unguessable token and hand the speakers a
        # hub-relative path — each resolves it against the hub host it is live
        # on, so it survives the hub's IP changing (see PaClipStore).
        token = _pa_store(self._hass).put(
            data, content_type or "application/octet-stream"
        )
        clip_path = f"/api/{DOMAIN}/audio/pa-clip/{token}"

        # Play(pa) on the selected speakers, or broadcast to all. A target that
        # isn't enrolled is skipped rather than failing the whole announcement.
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
        """Pull the ``audio`` (bounded) + optional ``targets`` parts.

        ``targets`` is an optional text field — a JSON array or comma-separated
        list of speaker ids — naming the speakers the clip should play on. It is
        normalised to canonical mac6s (invalid entries dropped); an empty list
        means "broadcast to all".

        Returns ``(filename, content_type, data, targets, None)`` or
        ``("", "", b"", [], error_response)``.
        """
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
    """GET /audio/pa-clip/{token} — serve a hosted PA clip to the speakers.

    No JWT: access is gated by the unguessable token + short TTL (presigned-URL
    pattern). Deliberately NOT LAN-gated — the speakers fetch this and, behind
    Docker, every inbound source is rewritten to a non-LAN peer, so a LAN gate
    would reject them. Plain HTTP so the Pi's ``wget`` needs no TLS.
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
    """GET/PUT /audio/athan — the hub-owned athan config.

    GET (``audio.read``) renders the app's athan settings screen. PUT
    (``audio.manage``) replaces it and relays it RETAINED to ``athan/config``
    so the scheduler picks it up immediately and on every reconnect.
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
        # The effective location the scheduler will use: the app's pinned coords
        # if any, else the hub's own home location (hass.config). The app renders
        # this read-only ("Location follows your home · <timezone>") now that it
        # no longer pins a city itself.
        cfg = self._hass.config
        pinned = athan.get("lat") is not None and athan.get("lon") is not None
        location = {
            "lat": athan.get("lat") if pinned else cfg.latitude,
            "lon": athan.get("lon") if pinned else cfg.longitude,
            "timezone": athan.get("timezone") or cfg.time_zone,
            "source": "config" if pinned else "home",
        }
        return self.json({"athan": athan, "location": location})

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
        # Relay retained so the scheduler gets it now and on every reconnect.
        # A dead bus is non-fatal here: the config is persisted, the next
        # adapter (re)connect does not re-push stored athan, so surface it.
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
        # Re-arm the hub's own scheduler off the new config immediately (the
        # retained relay above is now vestigial — kept for any external listener
        # — but the hub itself is the scheduler and reschedules in-process).
        scheduler = get_athan_scheduler(self._hass)
        if scheduler is not None:
            scheduler.reschedule()
        return self.json({"athan": stored, "relayed": relayed})


# -- installer: broker / PA credentials ---------------------------------------


class CasaSmartAudioBrokerView(_AudioView):
    """GET/PUT /audio/broker — the MQTT broker credentials (admin only).

    PUT cycles the adapter so the hub reconnects with the new creds. Body
    fields are all optional (omitted = unchanged): ``host``, ``port``,
    ``tls``, ``username``, ``password``.
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
        # Reconnect the single MQTT client with the new creds.
        adapter = get_audio_adapter(self._hass)
        if adapter is not None:
            await adapter.async_reconfigure()
        return self.json({"broker": _redact_secret(broker, "password")})


class CasaSmartAudioPaConfigView(_AudioView):
    """GET/PUT /audio/pa-config — the PA service host/port/api-key (admin)."""

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


# -- device-facing: the Pi pulls its broker creds on boot ---------------------


class CasaSmartAudioProvisionView(_AudioView):
    """GET /audio/provision — broker coordinates for the Pi speaker agent.

    Replaces the dead Supabase edge function the agent used to beg for creds.
    Auth is the shared provisioning secret (header ``X-CasaSmart-Provision-Key``,
    baked into the Pi image) OR LAN membership. The secret path works from any
    source — so a Docker-NAT'd hub, whose LAN check sees a rewritten peer IP,
    still provisions — while the LAN fallback keeps the original keyless posture
    for a bare on-LAN hub.
    """

    url = f"/api/{DOMAIN}/audio/provision"
    name = f"api:{DOMAIN}:audio:provision"

    async def get(self, request: web.Request) -> web.Response:
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        # Auth = the shared provisioning secret (header) OR the LAN gate. The
        # secret path is what a Pi uses: it works from any source, so a
        # Docker-NAT'd hub (whose LAN check sees a rewritten peer IP) still
        # provisions. The LAN fallback preserves the original keyless posture
        # for a bare on-LAN hub.
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


# -- helpers ------------------------------------------------------------------


def _parse_targets(raw: Any) -> list[str]:
    """Normalise a PA ``targets`` field to a de-duped list of canonical mac6s.

    Accepts a JSON array (``["aabbcc", ...]``) or a comma/space-separated
    string. Invalid ids are dropped silently — a bad selection must never block
    the announcement, it just falls back toward broadcast. Order preserved.
    """
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
    """Copy ``config`` with ``field`` reduced to a bool ``<field>_set``.

    Config GETs are admin-only, but the broker password / PA key still never
    need to round-trip to the app — the app only needs to know whether one is
    set. The plaintext stays hub-side (and goes to the Pi only over the
    LAN-only provision endpoint).
    """
    redacted = dict(config)
    redacted[f"{field}_set"] = bool(redacted.pop(field, None))
    return redacted


# -- executor jobs (storage-touching engine calls) ----------------------------
# Plain module-level callables so async_add_executor_job gets a function, not a
# closure capturing request state.


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
