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
- ``GET    /audio/provision``              — broker coordinates, LAN-only, no JWT

Mutations that move the hub's view of the speakers (enroll/remove/update) fire
``EVENT_AUDIO_CHANGED`` so the WS server nudges connected apps to re-fetch —
same pattern as ``EVENT_ALARM_CHANGED``. MQTT-driven changes already fire it
from the adapter's ingest path.
"""

from __future__ import annotations

import ipaddress
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .audio import (
    AudioEngine,
    AudioError,
    UnknownSpeakerError,
    TOPIC_ATHAN_CONFIG,
)
from .audio_adapter import AudioAdapter, AudioAdapterNotReady
from .auth_api import (
    authenticate_request,
    get_extra_lan_cidrs,
    is_lan_request,
    json_body,
)
from .const import DOMAIN, EVENT_AUDIO_CHANGED

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)

# A PA clip is small and the PA service is on the LAN; this is generous for a
# busy box yet far under anything the app would sit through.
_PA_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Hard cap on a proxied PA upload so a client can't stream an unbounded body
# through the hub into the PA service. PA clips are short voice/chime files.
_PA_MAX_BYTES = 16 * 1024 * 1024


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


class CasaSmartAudioSpeakersView(_AudioView):
    """GET /audio/speakers — enrolled speakers merged with live status.

    POST /audio/speakers — enroll a (discovered) speaker once it is on the LAN
    and named (the tail of the app's add-speaker flow).
    """

    url = f"/api/{DOMAIN}/audio/speakers"
    name = f"api:{DOMAIN}:audio:speakers"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "audio.read")
        if error is not None:
            return error
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        # speakers() copies the in-memory mirror — pure CPU, no executor hop.
        return self.json({"speakers": audio.speakers()})

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
            await self._hass.async_add_executor_job(audio.remove_speaker, mac6)
        except UnknownSpeakerError as err:
            return self.json_message(str(err), HTTPStatus.NOT_FOUND)
        except AudioError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._notify_change()
        return self.json({"deleted": mac6})


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
    """POST /audio/pa — proxy a PA audio upload to the PA service.

    The app sends a ``multipart/form-data`` body with an ``audio`` file part
    (same shape the PA service already accepts). The hub forwards it to the
    configured PA service ``/pa/upload`` with the stored ``X-API-Key``, so the
    phone never holds the PA key. The PA service hosts the clip + broadcasts it.
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
        pa = audio.get_pa()
        host = pa.get("host")
        if not host:
            return self.json_message(
                "PA service is not configured on the hub",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

        filename, content_type, data, read_error = await self._read_audio_part(
            request
        )
        if read_error is not None:
            return read_error

        target = f"http://{host}:{pa.get('port') or 9876}/pa/upload"
        form = aiohttp.FormData()
        form.add_field(
            "audio", data, filename=filename, content_type=content_type
        )
        headers = {}
        if pa.get("api_key"):
            headers["X-API-Key"] = pa["api_key"]
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                target, data=form, headers=headers, timeout=_PA_UPLOAD_TIMEOUT
            ) as response:
                body = await response.json(content_type=None)
                status = response.status
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("PA upload proxy to %s failed: %s", target, err)
            return self.json_message(
                f"PA service unreachable: {err}", HTTPStatus.BAD_GATEWAY
            )
        # Pass the PA service's own status/body straight back to the app.
        return web.json_response(
            body if isinstance(body, dict) else {"ok": status == HTTPStatus.OK},
            status=status,
        )

    async def _read_audio_part(
        self, request: web.Request
    ) -> tuple[str, str, bytes, web.Response | None]:
        """Pull the ``audio`` part out of the multipart body, bounded.

        Returns ``(filename, content_type, data, None)`` or
        ``("", "", b"", error_response)``.
        """
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError):
            return "", "", b"", self.json_message(
                "Body must be multipart/form-data with an 'audio' file",
                HTTPStatus.BAD_REQUEST,
            )
        async for part in reader:
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
                    return "", "", b"", self.json_message(
                        f"Audio too large (max {_PA_MAX_BYTES} bytes)",
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    )
                chunks.append(chunk)
            return filename, content_type, b"".join(chunks), None
        return "", "", b"", self.json_message(
            "Missing 'audio' file part", HTTPStatus.BAD_REQUEST
        )


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
        return self.json({"athan": audio.get_athan()})

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
    LAN-only with NO JWT (same posture as the tank ingest): the speaker lives
    on the LAN, has no user identity, and a leaked broker cred is useless
    remotely (the broker isn't exposed off-LAN). The response IS the secret —
    so it never leaves the local network.
    """

    url = f"/api/{DOMAIN}/audio/provision"
    name = f"api:{DOMAIN}:audio:provision"

    async def get(self, request: web.Request) -> web.Response:
        audio, not_ready = self._audio_or_503()
        if not_ready is not None:
            return not_ready
        if not is_lan_request(request, get_extra_lan_cidrs(self._hass)):
            _LOGGER.warning(
                "Audio provision refused (non-LAN source: %s)", request.remote
            )
            return self.json_message(
                "Provisioning is only available on the hub's own network",
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


def _enroll_job(audio: AudioEngine, mac, name, room):
    return audio.enroll_speaker(mac, name, room)


def _update_job(audio: AudioEngine, mac6, name, room):
    return audio.update_speaker(mac6, name=name, room=room)


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
