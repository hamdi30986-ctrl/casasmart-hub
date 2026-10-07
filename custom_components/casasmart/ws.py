"""CasaSmart WebSocket server (/api/casasmart/ws).

Authenticates with the first frame, then pushes room-scoped state changes and
content-free change nudges. Frame shapes live in ws_protocol.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.json import json_dumps

from . import ws_protocol
from .auth_api import get_engine
from .auth_engine import AuthEngine
from .auth_tokens import TokenError
from .const import (
    API_VERSION,
    DOMAIN,
    EVENT_ALARM_CHANGED,
    EVENT_AUDIO_CHANGED,
    EVENT_AUTH_CHANGED,
    EVENT_ENERGY_CHANGED,
    EVENT_REGISTRY_CHANGED,
    EVENT_SUGGESTIONS_CHANGED,
    EVENT_TANK_CHANGED,
    WS_AUTH_TIMEOUT,
    WS_CLOSE_AUTH_EXPIRED,
    WS_CLOSE_AUTH_FAILED,
    WS_CLOSE_AUTH_TIMEOUT,
    WS_CLOSE_TOO_SLOW,
    WS_REAUTH_GRACE,
    WS_SEND_QUEUE_MAX,
    WS_TOKEN_RECHECK,
)
from .filtering import in_scope, is_served, serialize_device

_LOGGER = logging.getLogger(__name__)

# The live connections of both listeners, so unload can close them.
_DATA_CONNECTIONS = f"{DOMAIN}_ws_connections"

# How long unload waits for a phone to answer the close before dropping it.
_CLOSE_TIMEOUT = 1.0


async def async_close_connections(hass: HomeAssistant) -> None:
    """Close every live CasaSmart WebSocket with "going away".

    Otherwise a socket would outlive the entry, and the TLS listener would
    wait for it on shutdown. The app reconnects once the hub is back.
    """
    connections = hass.data.get(_DATA_CONNECTIONS)
    if connections:
        await asyncio.gather(*(conn.async_close() for conn in list(connections)))


class CasaSmartWebSocketView(HomeAssistantView):
    """GET /api/casasmart/ws: the real-time push channel."""

    url = f"/api/{DOMAIN}/ws"
    name = f"api:{DOMAIN}:ws"
    # The token arrives in the first frame.
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        """Upgrade to a WebSocket and run the connection to completion."""
        ws = web.WebSocketResponse(heartbeat=55.0)
        await ws.prepare(request)
        connection = WsConnection(self._hass, ws, self._hub_version)
        connections = self._hass.data.setdefault(_DATA_CONNECTIONS, set())
        connections.add(connection)
        try:
            await connection.run()
        finally:
            connections.discard(connection)
            connection.cleanup()
        return ws


class WsConnection:
    """One authenticated socket.

    Event-loop listeners turn Home Assistant events into frames, a single
    sender task writes them from a coalescing queue, and a periodic re-check
    catches a token that was revoked or expired mid-connection.
    """

    def __init__(
        self, hass: HomeAssistant, ws: web.WebSocketResponse, hub_version: str
    ) -> None:
        self._hass = hass
        self._ws = ws
        self._hub_version = hub_version
        self._subscription = ws_protocol.Subscription()

        self._send_queue = ws_protocol.CoalescingSendQueue(WS_SEND_QUEUE_MAX)
        self._sender_task: asyncio.Task | None = None
        self._unsubs: list[Callable[[], None]] = []

        self._subscribed = False
        # Entities this connection was sent, in its snapshot or a state change.
        # Only these get an entity_removed: the id of any other would reveal it.
        self._shown: set[str] = set()
        self._token: str | None = None
        # Claims of the current token. None while a re-auth is pending, which
        # holds back every frame that carries home data.
        self._claims: dict[str, Any] | None = None
        # Closes the socket unless a fresh token arrives within the grace.
        self._reauth_deadline_task: asyncio.Task | None = None

    async def run(self) -> None:
        """Authenticate, then serve the connection until the socket closes."""
        if not await self._authenticate_first_frame():
            return

        self._sender_task = asyncio.create_task(self._sender_loop())
        for event_type, handler in (
            ("state_changed", self._on_state_changed),
            (EVENT_REGISTRY_CHANGED, self._on_registry_changed),
            (EVENT_SUGGESTIONS_CHANGED, self._on_suggestions_changed),
            (EVENT_ALARM_CHANGED, self._on_alarm_changed),
            (EVENT_AUDIO_CHANGED, self._on_audio_changed),
            (EVENT_ENERGY_CHANGED, self._on_energy_changed),
            (EVENT_TANK_CHANGED, self._on_tank_changed),
            (EVENT_AUTH_CHANGED, self._on_auth_changed),
        ):
            self._unsubs.append(self._hass.bus.async_listen(event_type, handler))
        recheck_task = asyncio.create_task(self._token_recheck_loop())
        try:
            await self._receive_loop()
        finally:
            recheck_task.cancel()

    async def async_close(self) -> None:
        """Close the socket with "going away", dropping it if the phone is slow."""
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await self._ws.close(
                    code=WSCloseCode.GOING_AWAY, message=b"hub unloading"
                )
        except TimeoutError:
            # aiohttp closes the transport when the close is cut short.
            pass

    def cleanup(self) -> None:
        """Drop every listener and cancel the connection's tasks (idempotent)."""
        while self._unsubs:
            self._unsubs.pop()()
        for task in (self._sender_task, self._reauth_deadline_task):
            if task is not None:
                task.cancel()
        self._sender_task = None
        self._reauth_deadline_task = None

    async def _authenticate_first_frame(self) -> bool:
        """Enforce the first-frame auth contract; True when authenticated."""
        try:
            async with asyncio.timeout(WS_AUTH_TIMEOUT):
                msg = await self._ws.receive()
        except TimeoutError:
            await self._ws.close(code=WS_CLOSE_AUTH_TIMEOUT, message=b"auth timeout")
            return False

        if msg.type != WSMsgType.TEXT:
            await self._ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"auth required")
            return False

        try:
            frame = msg.json()
            if ws_protocol.parse_client_frame(frame) != "auth":
                raise ws_protocol.ProtocolError("First frame must be 'auth'")
            token = ws_protocol.auth_token(frame)
        except (ValueError, ws_protocol.ProtocolError) as err:
            await self._ws.send_json(ws_protocol.frame_auth_failed(str(err)))
            await self._ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"auth failed")
            return False

        if not await self._async_validate_token(token):
            await self._ws.send_json(
                ws_protocol.frame_auth_failed("Invalid or expired token")
            )
            await self._ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"auth failed")
            return False

        self._token = token
        await self._ws.send_json(
            ws_protocol.frame_auth_ok(self._hub_version, API_VERSION)
        )
        return True

    async def _async_validate_token(self, token: str) -> bool:
        """Check a token and keep its claims; False unless valid with devices.read.

        The auth engine checks everything in memory, so this needs no executor.
        """
        engine = get_engine(self._hass)
        if engine is None:
            return False
        try:
            claims = engine.validate_token(token)
        except TokenError:
            return False
        if not AuthEngine.authorize(claims, "devices.read"):
            return False
        self._claims = claims
        return True

    async def _token_recheck_loop(self) -> None:
        """Re-check the token periodically, including any renewed token."""
        while not self._ws.closed:
            await asyncio.sleep(WS_TOKEN_RECHECK)
            if self._token and await self._async_validate_token(self._token):
                continue
            # Announce once per grace period; a valid auth frame cancels it.
            if self._reauth_deadline_task is None or self._reauth_deadline_task.done():
                await self._require_reauth()

    async def _require_reauth(self) -> None:
        """Send no more data until a new token arrives.

        Frames queued under the old claims are dropped. The subscription is
        kept, so a successful re-auth resumes it with a fresh snapshot.
        """
        self._token = None
        self._claims = None
        self._send_queue.drop_data()
        await self._enqueue(ws_protocol.frame_auth_required(int(WS_REAUTH_GRACE)))
        self._reauth_deadline_task = asyncio.create_task(self._reauth_deadline())

    async def _reauth_deadline(self) -> None:
        """Close the connection unless re-auth lands within the grace window."""
        await asyncio.sleep(WS_REAUTH_GRACE)
        if self._token is None and not self._ws.closed:
            await self._ws.close(code=WS_CLOSE_AUTH_EXPIRED, message=b"token expired")

    @callback
    def _on_auth_changed(self, event: Event) -> None:
        """Some device's access changed; re-check this connection's token now."""
        self._hass.async_create_task(self._recheck_now())

    async def _recheck_now(self) -> None:
        """Re-check the token once, as the recheck loop does."""
        if self._ws.closed or not self._token:
            return
        if await self._async_validate_token(self._token):
            return
        if self._reauth_deadline_task is None or self._reauth_deadline_task.done():
            await self._require_reauth()

    async def _receive_loop(self) -> None:
        """Handle client frames until the socket closes."""
        async for msg in self._ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                frame = msg.json()
                frame_type = ws_protocol.parse_client_frame(frame)
            except (ValueError, ws_protocol.ProtocolError) as err:
                await self._enqueue(ws_protocol.frame_error(str(err)))
                continue

            if frame_type == "ping":
                await self._enqueue(ws_protocol.frame_pong())
            elif frame_type == "subscribe":
                await self._handle_subscribe(frame)
            elif frame_type == "auth":
                await self._handle_reauth(frame)

    async def _handle_subscribe(self, frame: dict[str, Any]) -> None:
        """Set the subscription and ack with a snapshot of the current state."""
        try:
            entity_ids = ws_protocol.subscribe_entity_ids(frame)
        except ws_protocol.ProtocolError as err:
            await self._enqueue(ws_protocol.frame_error(str(err)))
            return
        self._subscription.set(entity_ids)
        self._subscribed = True
        await self._emit_snapshot()

    async def _emit_snapshot(self) -> None:
        """Send the subscribed frame with the served, in-scope subscribed devices.

        Skipped while re-auth is pending; the re-auth sends it instead.
        """
        if self._claims is None:
            return
        rooms = self._claims.get("rooms")
        devices = [
            serialize_device(self._hass, state)
            for state in self._hass.states.async_all()
            if is_served(self._hass, state.entity_id)
            and self._subscription.matches(state.entity_id)
            and in_scope(self._hass, state.entity_id, rooms)
        ]
        devices.sort(key=lambda device: device["entity_id"])
        self._shown = {device["entity_id"] for device in devices}
        await self._enqueue(ws_protocol.frame_subscribed(devices))

    async def _handle_reauth(self, frame: dict[str, Any]) -> None:
        """Handle an auth frame sent after the first one."""
        try:
            token = ws_protocol.auth_token(frame)
        except ws_protocol.ProtocolError as err:
            await self._enqueue(ws_protocol.frame_error(str(err)))
            return
        if not await self._async_validate_token(token):
            await self._enqueue(
                ws_protocol.frame_auth_failed("Invalid or expired token")
            )
            return
        self._token = token
        if self._reauth_deadline_task is not None:
            self._reauth_deadline_task.cancel()
            self._reauth_deadline_task = None
        await self._enqueue(ws_protocol.frame_auth_ok(self._hub_version, API_VERSION))
        # Nothing was sent while re-auth was pending, so resync the app (and
        # apply any change of rooms) with a fresh snapshot.
        if self._subscribed:
            await self._emit_snapshot()

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Push a subscribed, served, in-scope state change.

        Runs for every state change in HA, so the cheap checks come first.
        """
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if new_state is None:
            # Tell the app to drop the tile, even when its area went first.
            if entity_id in self._shown and self._offer_or_close(
                ws_protocol.frame_entity_removed(entity_id)
            ):
                self._shown.discard(entity_id)
            return
        if (
            not self._subscription.matches(entity_id)
            or not is_served(self._hass, entity_id)
            or not in_scope(self._hass, entity_id, (self._claims or {}).get("rooms"))
        ):
            return
        device = serialize_device(self._hass, new_state)
        if self._offer_or_close(ws_protocol.frame_state_changed(device)):
            self._shown.add(entity_id)

    @callback
    def _on_registry_changed(self, event: Event) -> None:
        """Nudge the app to re-fetch the registry (the GET is room-scoped)."""
        kind = event.data.get("kind", "registry")
        self._offer_or_close(ws_protocol.frame_registry_changed(kind))

    @callback
    def _on_suggestions_changed(self, event: Event) -> None:
        """Nudge subscribed apps to re-read their suggestions."""
        if self._subscribed:
            self._offer_or_close({"type": "suggestions_changed", "version": 1})

    @callback
    def _on_tank_changed(self, event: Event) -> None:
        """Nudge the app to re-fetch a tank's level (the GET checks access)."""
        device_id = event.data.get("device_id", "")
        self._offer_or_close(ws_protocol.frame_tank_changed(device_id))

    @callback
    def _on_alarm_changed(self, event: Event) -> None:
        """Nudge apps with alarm.read to re-fetch the alarm state.

        Even an empty nudge would reveal when the alarm is used, so other
        connections get nothing.
        """
        if not AuthEngine.authorize(self._claims or {}, "alarm.read"):
            return
        self._offer_or_close(ws_protocol.frame_alarm_changed())

    @callback
    def _on_audio_changed(self, event: Event) -> None:
        """Nudge apps with audio.read to re-fetch the speakers."""
        if not AuthEngine.authorize(self._claims or {}, "audio.read"):
            return
        self._offer_or_close(ws_protocol.frame_audio_changed())

    @callback
    def _on_energy_changed(self, event: Event) -> None:
        """Nudge apps with energy.read to re-fetch Energy Saving."""
        if not AuthEngine.authorize(self._claims or {}, "energy.read"):
            return
        self._offer_or_close(ws_protocol.frame_energy_changed())

    async def _enqueue(self, frame: dict[str, Any]) -> None:
        """Queue a protocol frame behind pending pushes, keeping send order."""
        self._send_queue.put_protocol(frame)

    def _offer_or_close(self, frame: dict[str, Any]) -> bool:
        """Queue a push frame, or close the socket if the app stopped reading.

        Nothing is queued while re-auth is pending. True when queued.
        """
        if self._claims is None:
            return False
        if self._send_queue.offer(frame):
            return True
        _LOGGER.warning("WS client not draining (protocol backlog), disconnecting")
        self._hass.async_create_task(
            self._ws.close(code=WS_CLOSE_TOO_SLOW, message=b"too slow")
        )
        return False

    async def _sender_loop(self) -> None:
        """The single socket writer: drains the queue in order."""
        try:
            while not self._ws.closed:
                frame = await self._send_queue.get()
                # HA state attributes can contain datetime values (notably
                # automation.last_triggered), so use the same encoder as REST.
                await self._ws.send_json(frame, dumps=json_dumps)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
