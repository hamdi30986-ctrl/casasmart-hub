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

Auth (B1.6): tokens are hub-issued CasaSmart JWTs, validated by the auth
engine (``auth_engine.validate_token``). The connection keeps the token's
claims; role is checked once (``devices.read``) and the ``rooms`` claim
scopes BOTH the subscribe snapshot and every push — a room-scoped user
receives ONLY their rooms' changes (plan B1.5 acceptance). The 60s
recheck loop now also catches plain JWT expiry, which triggers the same
``auth_required`` + 30s grace, so the app's silent re-auth (B9) slots in
without a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.json import json_dumps

from . import ws_protocol
from .auth_engine import AuthEngine
from .auth_tokens import TokenError
from .const import (
    API_VERSION,
    DOMAIN,
    EVENT_ALARM_CHANGED,
    EVENT_AUDIO_CHANGED,
    EVENT_AUTH_CHANGED,
    EVENT_REGISTRY_CHANGED,
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
        # Phase 11: a coalescing queue absorbs bursts on a congested tunnel —
        # newer state supersedes older, redundant nudges collapse, and the
        # oldest push is dropped before the socket ever is. Protocol frames are
        # never dropped (put_protocol); pushes go through offer().
        self._send_queue = ws_protocol.CoalescingSendQueue(WS_SEND_QUEUE_MAX)
        self._sender_task: asyncio.Task | None = None
        self._unsub_state_changed: Any = None
        self._unsub_registry_changed: Any = None
        self._unsub_alarm_changed: Any = None
        self._unsub_audio_changed: Any = None
        self._unsub_tank_changed: Any = None
        self._unsub_auth_changed: Any = None
        # True once the client has sent a `subscribe` (so a re-auth knows whether
        # to re-emit the snapshot); distinct from "subscribed to all", which the
        # Subscription models as entity_ids=None.
        self._subscribed = False
        self._token: str | None = None
        # Claims of the current token (role + room scope) — set on auth.
        self._claims: dict[str, Any] | None = None
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
        self._unsub_registry_changed = self._hass.bus.async_listen(
            EVENT_REGISTRY_CHANGED, self._on_registry_changed
        )
        self._unsub_alarm_changed = self._hass.bus.async_listen(
            EVENT_ALARM_CHANGED, self._on_alarm_changed
        )
        self._unsub_audio_changed = self._hass.bus.async_listen(
            EVENT_AUDIO_CHANGED, self._on_audio_changed
        )
        self._unsub_tank_changed = self._hass.bus.async_listen(
            EVENT_TANK_CHANGED, self._on_tank_changed
        )
        # A privilege change (role/rooms/unpair/revoke) is content-free, so the
        # connection rechecks its OWN token at once instead of waiting up to
        # WS_TOKEN_RECHECK — closing the window where a just-narrowed scope keeps
        # getting old-scope pushes (Phase 8).
        self._unsub_auth_changed = self._hass.bus.async_listen(
            EVENT_AUTH_CHANGED, self._on_auth_changed
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
        if self._unsub_registry_changed is not None:
            self._unsub_registry_changed()
            self._unsub_registry_changed = None
        if self._unsub_alarm_changed is not None:
            self._unsub_alarm_changed()
            self._unsub_alarm_changed = None
        if self._unsub_audio_changed is not None:
            self._unsub_audio_changed()
            self._unsub_audio_changed = None
        if self._unsub_tank_changed is not None:
            self._unsub_tank_changed()
            self._unsub_tank_changed = None
        if self._unsub_auth_changed is not None:
            self._unsub_auth_changed()
            self._unsub_auth_changed = None
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
        """Validate a CasaSmart JWT and refresh the connection's claims.

        The B1.6 auth engine: signature + expiry + role permission. Pure
        HMAC math — no executor hop. Returns False (never raises) so the
        callers' control flow stays identical to the B1.5 stopgap.
        """
        from .auth_api import get_engine  # local: auth_api is sibling glue

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

    @callback
    def _on_auth_changed(self, event: Event) -> None:
        """Some device's privileges changed (content-free event). Recheck OUR
        token now: a connection whose device was untouched revalidates cleanly
        (a no-op), a revoked/re-scoped one gets `auth_required` at once."""
        self._hass.async_create_task(self._recheck_now())

    async def _recheck_now(self) -> None:
        """Immediate, single-shot version of the recheck loop's body."""
        if self._ws.closed or not self._token:
            return
        if await self._async_validate_token(self._token):
            return  # still valid — our device wasn't the one that changed
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
        self._subscribed = True
        await self._emit_snapshot()

    async def _emit_snapshot(self) -> None:
        """Send the `subscribed` frame: the current scoped + served snapshot of
        the subscribed set. Re-emitted after a scope-changing re-auth so the
        live view reflects the new rooms at once (Phase 8)."""
        rooms = (self._claims or {}).get("rooms")
        devices = [
            serialize_device(self._hass, state)
            for state in self._hass.states.async_all()
            if is_served(self._hass, state.entity_id)
            and self._subscription.matches(state.entity_id)
            and in_scope(self._hass, state.entity_id, rooms)
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
        old_rooms = (self._claims or {}).get("rooms")
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
        # Re-send the snapshot after EVERY re-auth (m2). The app gates data
        # frames while it re-authenticates (auth_required -> auth_ok), so any
        # state_changed the hub pushed during that grace window is dropped
        # client-side and its tiles freeze until the next reconnect. A fresh
        # snapshot on re-auth reconciles that missed state — and also drops/gains
        # rooms at once when an admin re-scoped this user mid-connection (the
        # scope-change case this used to be limited to; Phase 8).
        if self._subscribed:
            await self._emit_snapshot()

    # -- push ---------------------------------------------------------------

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """HA event-loop callback: queue a push if subscribed AND served.

        Runs for every state change in HA, so the cheap checks come first
        and serialization only happens for entities actually being pushed.
        """
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if new_state is None:
            # Entity removed (unpair / integration drop / registry delete). Tell
            # a subscribed app to drop the tile — otherwise a dead card lingers
            # for the connection's lifetime (Phase 8). The frame is just the id,
            # so there is no state to room-scope.
            if entity_id and self._subscription.matches(entity_id):
                self._offer_or_close(ws_protocol.frame_entity_removed(entity_id))
            return
        if (
            not self._subscription.matches(entity_id)
            or not is_served(self._hass, entity_id)
            # Room scope (B1.6): scoped tokens only get their rooms' pushes.
            or not in_scope(self._hass, entity_id, (self._claims or {}).get("rooms"))
        ):
            return
        device = serialize_device(self._hass, new_state)
        self._offer_or_close(ws_protocol.frame_state_changed(device))

    @callback
    def _on_registry_changed(self, event: Event) -> None:
        """B17: the home's organization changed — nudge the app to re-fetch.

        The frame carries only the change kind, never content, so there
        is nothing to room-scope here: the app's follow-up registry GET
        is filtered to its own slice like every other read.
        """
        kind = event.data.get("kind", "registry")
        self._offer_or_close(ws_protocol.frame_registry_changed(kind))

    @callback
    def _on_tank_changed(self, event: Event) -> None:
        """Phase 4: a tank reading landed — nudge the app to re-fetch its
        calibrated level. Content-free beyond the device id, like the registry
        nudge; the app's tank GET stays the authorization boundary."""
        device_id = event.data.get("device_id", "")
        self._offer_or_close(ws_protocol.frame_tank_changed(device_id))

    @callback
    def _on_alarm_changed(self, event: Event) -> None:
        """B13: arm state changed — nudge alarm-authorized apps to re-fetch.

        Gated by role: the socket only authorized on ``devices.read``, but a
        plain user has NO alarm access (plan roles table). Pushing even a
        content-free nudge to them would leak the timing of alarm activity,
        so connections whose claims lack ``alarm.read`` are skipped entirely.
        The frame carries no state — the app re-reads the gated state GET.
        """
        if not AuthEngine.authorize(self._claims or {}, "alarm.read"):
            return
        self._offer_or_close(ws_protocol.frame_alarm_changed())

    @callback
    def _on_audio_changed(self, event: Event) -> None:
        """B14: the hub's speaker view changed — nudge audio-authorized apps
        to re-fetch. Gated like ``_on_alarm_changed``: the socket authorized on
        ``devices.read``, but only connections whose claims carry ``audio.read``
        get the (content-free) nudge. The app re-reads the gated speakers GET.
        """
        if not AuthEngine.authorize(self._claims or {}, "audio.read"):
            return
        self._offer_or_close(ws_protocol.frame_audio_changed())

    async def _enqueue(self, frame: dict[str, Any]) -> None:
        """Route protocol replies through the same queue as pushes, preserving
        send order on the single writer. Protocol frames are loss-intolerant, so
        they are always admitted (never coalesced/dropped like pushes)."""
        self._send_queue.put_protocol(frame)

    def _offer_or_close(self, frame: dict[str, Any]) -> None:
        """Queue a push frame, coalescing/dropping under backpressure. Only a
        queue full of undrained protocol frames — a genuinely dead consumer —
        closes the socket (Phase 11: a burst no longer kills a healthy app)."""
        if self._send_queue.offer(frame):
            return
        _LOGGER.warning("WS client not draining (protocol backlog), disconnecting")
        self._hass.async_create_task(
            self._ws.close(code=WS_CLOSE_TOO_SLOW, message=b"too slow")
        )

    async def _sender_loop(self) -> None:
        """The single socket writer — drains the queue in order."""
        try:
            while not self._ws.closed:
                frame = await self._send_queue.get()
                # HA state attributes can contain datetime values (notably
                # automation.last_triggered), so use the same encoder as REST.
                await self._ws.send_json(frame, dumps=json_dumps)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
