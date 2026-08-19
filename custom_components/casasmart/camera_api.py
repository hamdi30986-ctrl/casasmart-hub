"""CasaSmart runtime component."""

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



SNAPSHOT_TIMEOUT = 10




PROXY_TIMEOUT = ClientTimeout(total=30)


def _ticket_store(hass: HomeAssistant) -> StreamTicketStore:
    """CasaSmart runtime component."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "camera_stream_tickets", StreamTicketStore()
    )


def _serves_camera(hass: HomeAssistant, entity_id: str, rooms) -> bool:
    """CasaSmart runtime component."""
    state = hass.states.get(entity_id)
    return (
        state is not None
        and entity_id.startswith("camera.")
        and is_served(hass, entity_id)
        and in_scope(hass, entity_id, rooms)
    )


class CasaSmartCameraSnapshotView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/camera/{{entity_id}}/snapshot"
    name = f"api:{DOMAIN}:camera:snapshot"
    requires_auth = False

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/camera/{{entity_id}}/stream"
    name = f"api:{DOMAIN}:camera:stream"
    requires_auth = False

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/camera/{{entity_id}}/hls/{{ticket}}/{{filename:[A-Za-z0-9_./]+}}"
    name = f"api:{DOMAIN}:camera:hls"
    requires_auth = False

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


        base = endpoint.rsplit("/", 1)[0]
        scheme = "https" if self._hass.http.ssl_certificate else "http"
        upstream = (
            f"{scheme}://localhost:{self._hass.http.server_port}"
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
