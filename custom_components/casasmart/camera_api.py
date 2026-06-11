"""Camera endpoints (Track B — B16 stage 3c-3).

The REST surface that replaces the app's last two raw-token camera
paths: the ``camera/stream`` WebSocket mint (the fullscreen HLS
fallback) and the ``entity_picture`` + bearer-header thumbnail fetch.
The PRIMARY viewing path — direct go2rtc on the LAN — never touched HA
and stays exactly as it was.

- ``GET /api/casasmart/camera/{entity_id}/snapshot`` — one JPEG frame,
  for the grid thumbnails. JWT-gated (``cameras.view``): the app's image
  widget can attach headers, so this is a normal authenticated endpoint.
- ``GET /api/casasmart/camera/{entity_id}/stream`` — mints an HLS stream
  for the entity and answers with a hub-proxied playlist URL. JWT-gated.
- ``GET /api/casasmart/camera/{entity_id}/hls/{ticket}/{filename}`` —
  the proxy itself. The WebView player fetches playlists and segments
  with NO headers, so auth rides in the path: a short-lived single-entity
  ticket minted by the stream endpoint (``camera_streams.py``). This is
  the same model as HA's own ``/api/hls/<token>/...`` — and it's what
  makes the fallback work through the tunnel and the TLS listener, where
  HA's HLS endpoint isn't reachable.

Every view passes the entity through the SAME gates as the device views
(camera domain + ``is_served`` + ``in_scope``), and out-of-scope is the
same 404 as nonexistent — room-scoped tokens can't enumerate cameras
they weren't granted.
"""

from __future__ import annotations

import logging
import time
from http import HTTPStatus

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.camera import async_get_image, async_request_stream
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth_api import authenticate_request
from .camera_streams import (
    TICKET_TTL,
    StreamTicketStore,
    TicketError,
    is_valid_hls_filename,
)
from .const import DOMAIN
from .filtering import in_scope, is_served
from .tunnel import TUNNEL_URL_CONFIG_KEY, normalize_tunnel_url

_LOGGER = logging.getLogger(__name__)

# One frame for a 400px grid tile; cameras that take longer than this
# are effectively offline for thumbnail purposes.
SNAPSHOT_TIMEOUT = 10

# Loopback fetch budget per HLS artifact. Playlist long-polls (LL-HLS
# ``_HLS_msn``) can legitimately hold for a part duration; segments are
# instant. One generous cap covers both.
PROXY_TIMEOUT = ClientTimeout(total=30)


def _ticket_store(hass: HomeAssistant) -> StreamTicketStore:
    """The ONE ticket store, shared across listeners and view instances.

    ``build_views`` constructs fresh view objects for the plain and TLS
    listeners — a ticket minted on either must validate on both, so the
    store lives in ``hass.data``, never on a view.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "camera_stream_tickets", StreamTicketStore()
    )


def _serves_camera(hass: HomeAssistant, entity_id: str, rooms) -> bool:
    """The shared existence gate: camera domain, served, in scope."""
    state = hass.states.get(entity_id)
    return (
        state is not None
        and entity_id.startswith("camera.")
        and is_served(hass, entity_id)
        and in_scope(hass, entity_id, rooms)
    )


class CasaSmartCameraSnapshotView(HomeAssistantView):
    """GET /api/casasmart/camera/{entity_id}/snapshot — one JPEG frame."""

    url = f"/api/{DOMAIN}/camera/{{entity_id}}/snapshot"
    name = f"api:{DOMAIN}:camera:snapshot"
    requires_auth = False  # CasaSmart JWT gate (B1.6)

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "cameras.view")
        if error is not None:
            return error
        if not _serves_camera(self._hass, entity_id, claims.get("rooms")):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        try:
            image = await async_get_image(
                self._hass, entity_id, timeout=SNAPSHOT_TIMEOUT
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Snapshot for %s failed: %s", entity_id, err)
            return self.json_message(
                f"Snapshot failed: {err}", HTTPStatus.BAD_GATEWAY
            )
        return web.Response(body=image.content, content_type=image.content_type)


class CasaSmartCameraStreamView(HomeAssistantView):
    """GET /api/casasmart/camera/{entity_id}/stream — mint the HLS fallback."""

    url = f"/api/{DOMAIN}/camera/{{entity_id}}/stream"
    name = f"api:{DOMAIN}:camera:stream"
    requires_auth = False  # CasaSmart JWT gate (B1.6)

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "cameras.view")
        if error is not None:
            return error
        if not _serves_camera(self._hass, entity_id, claims.get("rooms")):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        try:
            # Starts (or reuses) the camera's HLS stream worker. The
            # returned HA endpoint is NOT handed to the app — the proxy
            # view re-derives it per fetch, so a worker restart with a
            # fresh stream token never strands the player mid-watch.
            await async_request_stream(self._hass, entity_id, fmt="hls")
        except HomeAssistantError as err:
            _LOGGER.warning("Stream mint for %s failed: %s", entity_id, err)
            return self.json_message(
                f"Stream unavailable: {err}", HTTPStatus.BAD_GATEWAY
            )
        ticket = _ticket_store(self._hass).mint(entity_id, now=time.time())
        path = (
            f"/api/{DOMAIN}/camera/{entity_id}"
            f"/hls/{ticket.ticket_id}/master_playlist.m3u8"
        )
        body: dict = {"url": path, "expires_in": int(TICKET_TTL)}
        # The player is a WebView — it trusts public CAs, not the hub's
        # pinned LAN cert. Advertise the tunnel-absolute URL when one is
        # configured so the app can hand the WebView a URL whose cert
        # actually validates; the ticket store is shared, so a mint on
        # the LAN route plays fine through the tunnel.
        tunnel_url = self._tunnel_url()
        if tunnel_url is not None:
            body["tunnel_url"] = f"{tunnel_url}{path}"
        return self.json(body)

    def _tunnel_url(self) -> str | None:
        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return None
        raw = entries[0].runtime_data.hub_config.get(TUNNEL_URL_CONFIG_KEY)
        return normalize_tunnel_url(raw)


class CasaSmartCameraHlsProxyView(HomeAssistantView):
    """GET /api/casasmart/camera/{entity_id}/hls/{ticket}/{filename} — the proxy.

    Header-less by design (the WebView player can't attach any); the
    ticket in the path is the auth. Each request re-asks the camera
    component for the CURRENT HLS endpoint and loopback-fetches the
    artifact from HA's own port, so the app never sees an HA stream
    token and the playlist's relative segment URIs resolve right back
    into this proxy.
    """

    url = f"/api/{DOMAIN}/camera/{{entity_id}}/hls/{{ticket}}/{{filename:[A-Za-z0-9_./]+}}"
    name = f"api:{DOMAIN}:camera:hls"
    requires_auth = False  # ticket in path IS the auth (see module docstring)

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(
        self, request: web.Request, entity_id: str, ticket: str, filename: str
    ) -> web.Response:
        try:
            _ticket_store(self._hass).validate(
                ticket, entity_id, now=time.time()
            )
        except TicketError as err:
            return self.json_message(str(err), HTTPStatus.UNAUTHORIZED)
        # The ticket was minted through the full JWT + scope gate, but the
        # world can change in 15 minutes — re-check served-ness so an
        # entity hidden mid-watch goes dark immediately.
        if not _serves_camera(self._hass, entity_id, None):
            return self.json_message(
                f"Device {entity_id!r} not found", HTTPStatus.NOT_FOUND
            )
        if not is_valid_hls_filename(filename):
            return self.json_message(
                "Invalid stream path", HTTPStatus.BAD_REQUEST
            )

        try:
            endpoint = await async_request_stream(self._hass, entity_id, fmt="hls")
        except HomeAssistantError as err:
            return self.json_message(
                f"Stream unavailable: {err}", HTTPStatus.BAD_GATEWAY
            )
        # endpoint is ".../master_playlist.m3u8" — swap in the requested
        # artifact, keep the player's query string (LL-HLS long-poll params).
        base = endpoint.rsplit("/", 1)[0]
        scheme = "https" if self._hass.http.ssl_certificate else "http"
        upstream = (
            f"{scheme}://127.0.0.1:{self._hass.http.server_port}"
            f"{base}/{filename}"
        )
        if request.query_string:
            upstream = f"{upstream}?{request.query_string}"

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                upstream, timeout=PROXY_TIMEOUT, ssl=False
            ) as resp:
                body = await resp.read()
                if resp.status != HTTPStatus.OK:
                    _LOGGER.debug(
                        "HLS upstream %s for %s/%s", resp.status, entity_id, filename
                    )
                    return self.json_message(
                        "Stream artifact unavailable", HTTPStatus.BAD_GATEWAY
                    )
                return web.Response(
                    body=body,
                    content_type=resp.content_type,
                    headers={"Cache-Control": "no-store"},
                )
        except (ClientError, TimeoutError) as err:
            _LOGGER.warning("HLS proxy fetch failed for %s: %s", entity_id, err)
            return self.json_message(
                "Stream artifact unavailable", HTTPStatus.BAD_GATEWAY
            )
