"""CasaSmart WebSocket server (Track B — B1.5): real-time state push.

Replaces Supabase realtime for connected apps. One endpoint:

- ``GET /api/casasmart/ws`` — upgraded to a WebSocket. HA's built-in
  bearer auth is bypassed (``requires_auth = False``) because auth is the
  FIRST FRAME, never the URL — URLs leak into access logs and proxies
  (plan, audit round 5).

Connection lifecycle:

1. Client connects, sends ``{"type": "auth", "token": ...}`` within 30s
   or the server closes (4000).
2. Token validated -> ``auth_ok``; invalid -> ``auth_failed`` + close (4001).
3. Client sends ``subscribe`` -> ``subscribed`` ack with a snapshot;
   thereafter every matching ``state_changed`` in HA is pushed within ~1s.
4. Token re-checked every 60s; revoked/expired -> ``auth_required`` with a
   30s grace to send a fresh ``auth`` frame, else clean close (4002).

Every outgoing device passes through ``filtering.is_served`` /
``filtering.serialize_device`` — the exact same functions the REST views
use, so the socket can never leak what REST hides.

Auth note: tokens are HA bearer tokens for now, validated against HA's
auth backend (same stopgap as the REST views). B1.6 swaps
``_async_validate_token`` for the CasaSmart auth engine (device keypairs
-> JWT + role/room-scope enforcement) — the protocol does not change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Event, HomeAssistant, callback

from . import ws_protocol
from .const import (
    API_VERSION,
    DOMAIN,
    WS_AUTH_TIMEOUT,
    WS_CLOSE_AUTH_EXPIRED,
    WS_CLOSE_AUTH_FAILED,
    WS_CLOSE_AUTH_TIMEOUT,
    WS_CLOSE_TOO_SLOW,
    WS_REAUTH_GRACE,
    WS_SEND_QUEUE_MAX,
    WS_TOKEN_RECHECK,
)
from .filtering import is_served, serialize_device

_LOGGER = logging.getLogger(__name__)


class CasaSmartWebSocketView(HomeAssistantView):
    """GET /api/casasmart/ws — the real-time push channel."""

    url = f"/api/{DOMAIN}/ws"
    name = f"api:{DOMAIN}:ws"
    # Auth happens in-band (first frame), not via HA's bearer middleware.
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        """Upgrade to a WebSocket and run the connection to completion."""
        ws = web.WebSocketResponse(heartbeat=55.0)
        await ws.prepare(request)
        connection = WsConnection(self._hass, ws, self._hub_version)
        try:
            await connection.run()
        finally:
            connection.cleanup()
        return ws


class WsConnection:
    """One authenticated app connection: auth gate, subscription, push."""

    def __init__(
        self, hass: HomeAssistant, ws: web.WebSocketResponse, hub_version: str
    ) -> None:
        self._hass = hass
        self._ws = ws
        self._hub_version = hub_version
        self._subscription = ws_protocol.Subscription()
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=WS_SEND_QUEUE_MAX
        )
        self._sender_task: asyncio.Task | None = None
        self._unsub_state_changed: Any = None
        self._token: str | None = None
        # Set while the connection is inside an `auth_required` grace window.
        self._reauth_deadline_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Auth gate, then the receive loop until the socket closes."""
        if not await self._authenticate_first_frame():
            return

        self._sender_task = asyncio.create_task(self._sender_loop())
        self._unsub_state_changed = self._hass.bus.async_listen(
            "state_changed", self._on_state_changed
        )
        recheck_task = asyncio.create_task(self._token_recheck_loop())
        try:
            await self._receive_loop()
        finally:
            recheck_task.cancel()

    def cleanup(self) -> None:
        """Release listeners and tasks (idempotent, called on any exit)."""
        if self._unsub_state_changed is not None:
            self._unsub_state_changed()
            self._unsub_state_changed = None
        for task in (self._sender_task, self._reauth_deadline_task):
            if task is not None:
                task.cancel()
        self._sender_task = None
        self._reauth_deadline_task = None

    # -- auth ---------------------------------------------------------------

    async def _authenticate_first_frame(self) -> bool:
        """Enforce the first-frame auth contract; True when authenticated."""
        try:
            async with asyncio.timeout(WS_AUTH_TIMEOUT):
                msg = await self._ws.receive()
        except TimeoutError:
            await self._ws.close(
                code=WS_CLOSE_AUTH_TIMEOUT, message=b"auth timeout"
            )
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
        """Validate a bearer token against HA's auth backend.

        B1.6 replaces this with the CasaSmart auth engine (JWT signature +
        role/room-scope). Single seam on purpose.
        """
        refresh_token = self._hass.auth.async_validate_access_token(token)
        return refresh_token is not None

    async def _token_recheck_loop(self) -> None:
        """Catch mid-connection revocation/expiry (plan: auth_required + 30s).

        Keeps running after a successful re-auth so a renewed token is
        itself re-checked on the same cadence.
        """
        while not self._ws.closed:
            await asyncio.sleep(WS_TOKEN_RECHECK)
            if self._token and await self._async_validate_token(self._token):
                continue
            # Token died mid-connection: announce once, arm the grace
            # deadline. A fresh valid `auth` frame in the receive loop
            # disarms it; don't re-announce while a grace window is open.
            if (
                self._reauth_deadline_task is None
                or self._reauth_deadline_task.done()
            ):
                self._token = None
                await self._enqueue(
                    ws_protocol.frame_auth_required(int(WS_REAUTH_GRACE))
                )
                self._reauth_deadline_task = asyncio.create_task(
                    self._reauth_deadline()
                )

    async def _reauth_deadline(self) -> None:
        """Close the connection unless re-auth lands within the grace window."""
        await asyncio.sleep(WS_REAUTH_GRACE)
        if self._token is None and not self._ws.closed:
            await self._ws.close(
                code=WS_CLOSE_AUTH_EXPIRED, message=b"token expired"
            )

    # -- receive ------------------------------------------------------------

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
        devices = [
            serialize_device(self._hass, state)
            for state in self._hass.states.async_all()
            if is_served(self._hass, state.entity_id)
            and self._subscription.matches(state.entity_id)
        ]
        devices.sort(key=lambda device: device["entity_id"])
        await self._enqueue(ws_protocol.frame_subscribed(devices))

    async def _handle_reauth(self, frame: dict[str, Any]) -> None:
        """A mid-connection auth frame — answers `auth_required`."""
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
        await self._enqueue(
            ws_protocol.frame_auth_ok(self._hub_version, API_VERSION)
        )

    # -- push ---------------------------------------------------------------

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """HA event-loop callback: queue a push if subscribed AND served.

        Runs for every state change in HA, so the cheap checks come first
        and serialization only happens for entities actually being pushed.
        """
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if (
            new_state is None  # entity removed — not a device update
            or not self._subscription.matches(entity_id)
            or not is_served(self._hass, entity_id)
        ):
            return
        device = serialize_device(self._hass, new_state)
        try:
            self._send_queue.put_nowait(ws_protocol.frame_state_changed(device))
        except asyncio.QueueFull:
            # Consumer is 256 frames behind: dead link or hopeless backlog.
            # Close instead of buffering unbounded; the app reconnects.
            _LOGGER.warning("WS client too slow (queue full), disconnecting")
            self._hass.async_create_task(
                self._ws.close(code=WS_CLOSE_TOO_SLOW, message=b"too slow")
            )

    async def _enqueue(self, frame: dict[str, Any]) -> None:
        """Route protocol replies through the same queue as pushes,
        preserving send order on the single writer."""
        await self._send_queue.put(frame)

    async def _sender_loop(self) -> None:
        """The single socket writer — drains the queue in order."""
        try:
            while not self._ws.closed:
                frame = await self._send_queue.get()
                await self._ws.send_json(frame)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
