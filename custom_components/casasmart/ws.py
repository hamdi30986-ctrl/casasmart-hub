"""CasaSmart runtime component."""

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
    EVENT_ENERGY_CHANGED,
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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/ws"
    name = f"api:{DOMAIN}:ws"

    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        """CasaSmart runtime component."""
        ws = web.WebSocketResponse(heartbeat=55.0)
        await ws.prepare(request)
        connection = WsConnection(self._hass, ws, self._hub_version)
        try:
            await connection.run()
        finally:
            connection.cleanup()
        return ws


class WsConnection:
    """CasaSmart runtime component."""

    def __init__(
        self, hass: HomeAssistant, ws: web.WebSocketResponse, hub_version: str
    ) -> None:
        self._hass = hass
        self._ws = ws
        self._hub_version = hub_version
        self._subscription = ws_protocol.Subscription()




        self._send_queue = ws_protocol.CoalescingSendQueue(WS_SEND_QUEUE_MAX)
        self._sender_task: asyncio.Task | None = None
        self._unsub_state_changed: Any = None
        self._unsub_registry_changed: Any = None
        self._unsub_alarm_changed: Any = None
        self._unsub_audio_changed: Any = None
        self._unsub_energy_changed: Any = None
        self._unsub_tank_changed: Any = None
        self._unsub_auth_changed: Any = None



        self._subscribed = False
        self._token: str | None = None

        self._claims: dict[str, Any] | None = None

        self._reauth_deadline_task: asyncio.Task | None = None



    async def run(self) -> None:
        """CasaSmart runtime component."""
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
        self._unsub_energy_changed = self._hass.bus.async_listen(
            EVENT_ENERGY_CHANGED, self._on_energy_changed
        )
        self._unsub_tank_changed = self._hass.bus.async_listen(
            EVENT_TANK_CHANGED, self._on_tank_changed
        )




        self._unsub_auth_changed = self._hass.bus.async_listen(
            EVENT_AUTH_CHANGED, self._on_auth_changed
        )
        recheck_task = asyncio.create_task(self._token_recheck_loop())
        try:
            await self._receive_loop()
        finally:
            recheck_task.cancel()

    def cleanup(self) -> None:
        """CasaSmart runtime component."""
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
        if self._unsub_energy_changed is not None:
            self._unsub_energy_changed()
            self._unsub_energy_changed = None
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



    async def _authenticate_first_frame(self) -> bool:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        from .auth_api import get_engine

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
        """CasaSmart runtime component."""
        while not self._ws.closed:
            await asyncio.sleep(WS_TOKEN_RECHECK)
            if self._token and await self._async_validate_token(self._token):
                continue



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
        """CasaSmart runtime component."""
        await asyncio.sleep(WS_REAUTH_GRACE)
        if self._token is None and not self._ws.closed:
            await self._ws.close(
                code=WS_CLOSE_AUTH_EXPIRED, message=b"token expired"
            )

    @callback
    def _on_auth_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        self._hass.async_create_task(self._recheck_now())

    async def _recheck_now(self) -> None:
        """CasaSmart runtime component."""
        if self._ws.closed or not self._token:
            return
        if await self._async_validate_token(self._token):
            return
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



    async def _receive_loop(self) -> None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        try:
            entity_ids = ws_protocol.subscribe_entity_ids(frame)
        except ws_protocol.ProtocolError as err:
            await self._enqueue(ws_protocol.frame_error(str(err)))
            return
        self._subscription.set(entity_ids)
        self._subscribed = True
        await self._emit_snapshot()

    async def _emit_snapshot(self) -> None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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







        if self._subscribed:
            await self._emit_snapshot()



    @callback
    def _on_state_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if new_state is None:




            if entity_id and self._subscription.matches(entity_id):
                self._offer_or_close(ws_protocol.frame_entity_removed(entity_id))
            return
        if (
            not self._subscription.matches(entity_id)
            or not is_served(self._hass, entity_id)

            or not in_scope(self._hass, entity_id, (self._claims or {}).get("rooms"))
        ):
            return
        device = serialize_device(self._hass, new_state)
        self._offer_or_close(ws_protocol.frame_state_changed(device))

    @callback
    def _on_registry_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        kind = event.data.get("kind", "registry")
        self._offer_or_close(ws_protocol.frame_registry_changed(kind))

    @callback
    def _on_tank_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        device_id = event.data.get("device_id", "")
        self._offer_or_close(ws_protocol.frame_tank_changed(device_id))

    @callback
    def _on_alarm_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        if not AuthEngine.authorize(self._claims or {}, "alarm.read"):
            return
        self._offer_or_close(ws_protocol.frame_alarm_changed())

    @callback
    def _on_audio_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        if not AuthEngine.authorize(self._claims or {}, "audio.read"):
            return
        self._offer_or_close(ws_protocol.frame_audio_changed())

    @callback
    def _on_energy_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        if not AuthEngine.authorize(self._claims or {}, "energy.read"):
            return
        self._offer_or_close(ws_protocol.frame_energy_changed())

    async def _enqueue(self, frame: dict[str, Any]) -> None:
        """CasaSmart runtime component."""
        self._send_queue.put_protocol(frame)

    def _offer_or_close(self, frame: dict[str, Any]) -> None:
        """CasaSmart runtime component."""
        if self._send_queue.offer(frame):
            return
        _LOGGER.warning("WS client not draining (protocol backlog), disconnecting")
        self._hass.async_create_task(
            self._ws.close(code=WS_CLOSE_TOO_SLOW, message=b"too slow")
        )

    async def _sender_loop(self) -> None:
        """CasaSmart runtime component."""
        try:
            while not self._ws.closed:
                frame = await self._send_queue.get()


                await self._ws.send_json(frame, dumps=json_dumps)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
