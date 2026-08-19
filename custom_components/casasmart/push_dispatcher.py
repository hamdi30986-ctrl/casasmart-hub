"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from base64 import b64encode
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Optional

import aiohttp

from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    EVENT_ALARM_TRIGGERED,
    EVENT_TANK_LOW,
    EVENT_TANK_OFFLINE,
    PUSH_RELAY_TIMEOUT_SECONDS,
    PUSH_TYPE_TANK_LOW,
    PUSH_TYPE_TANK_OFFLINE,
    PUSH_TYPE_UPDATE_WIDGETS,
)

if TYPE_CHECKING:
    from .push import PushTokenStore
    from .push_crypto import PushSigner
    from .tank import TankEngine

_LOGGER = logging.getLogger(__name__)



PUSH_TYPE_SECURITY = "security"
PUSH_TYPE_LOCK = "lock"



PUSH_TYPE_DEVICE_PAIRED = "device_paired"





_OWNER_ONLY_TYPES = frozenset(
    {
        PUSH_TYPE_SECURITY,
        PUSH_TYPE_LOCK,
        PUSH_TYPE_TANK_LOW,
        PUSH_TYPE_TANK_OFFLINE,
        PUSH_TYPE_DEVICE_PAIRED,
    }
)


PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"





STATE_LOCKED = "locked"
STATE_UNLOCKED = "unlocked"



_LOCK_PREFIX = "lock."
_LOCK_SETTLED = frozenset({STATE_LOCKED, STATE_UNLOCKED})
_LOCK_FLAP_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})







_WIDGET_DOMAINS = frozenset(
    {"light", "switch", "input_boolean", "lock", "cover", "climate", "fan"}
)




_WIDGET_PUSH_COALESCE_SECONDS = 15.0

_WIDGET_UNSETTLED = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})



_NONCE_BYTES = 32


class PushDispatcher:
    """CasaSmart runtime component."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        push_store: "PushTokenStore",
        signer: "PushSigner",
        hub_id: str,
        relay_url: str,
        session: aiohttp.ClientSession,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._hass = hass
        self._push_store = push_store
        self._signer = signer
        self._hub_id = hub_id
        self._relay_url = relay_url
        self._session = session
        self._clock = clock
        self._unsub_alarm: Optional[Callable[[], None]] = None
        self._unsub_state: Optional[Callable[[], None]] = None

        self._widget_flush_cancel: Optional[Callable[[], None]] = None
        self._active = False
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def relay_url(self) -> str:
        """CasaSmart runtime component."""
        return self._relay_url



    @callback
    def async_start(self) -> None:
        """CasaSmart runtime component."""
        self._active = True
        self._unsub_alarm = self._hass.bus.async_listen(
            EVENT_ALARM_TRIGGERED, self._on_alarm_triggered
        )
        self._unsub_state = self._hass.bus.async_listen(
            "state_changed", self._on_state_changed
        )
        _LOGGER.info(
            "Push dispatcher started (relay=%s, hub_id=%s)",
            self._relay_url,
            self._hub_id,
        )

    @callback
    def async_stop(self) -> None:
        """CasaSmart runtime component."""
        self._active = False
        if self._unsub_alarm is not None:
            self._unsub_alarm()
            self._unsub_alarm = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._widget_flush_cancel is not None:
            self._widget_flush_cancel()
            self._widget_flush_cancel = None
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    @callback
    def _schedule_dispatch(self, coro: Any) -> None:
        """CasaSmart runtime component."""
        task = self._hass.async_create_task(coro)
        if isinstance(task, asyncio.Task):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)



    @callback
    def _on_alarm_triggered(self, event: Event) -> None:
        """CasaSmart runtime component."""
        data = self._build_security_payload(event.data or {})
        self._schedule_dispatch(self._dispatch(data, PRIORITY_CRITICAL))

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")


        if entity_id.startswith(_LOCK_PREFIX):
            if self._is_real_lock_transition(old_state, new_state):
                data = self._build_lock_payload(entity_id, new_state)
                self._schedule_dispatch(self._dispatch(data, PRIORITY_NORMAL))




        if self._is_widget_relevant_change(entity_id, old_state, new_state):
            self._mark_widgets_dirty()

    @staticmethod
    def _is_widget_relevant_change(
        entity_id: str, old_state: Any, new_state: Any
    ) -> bool:
        """CasaSmart runtime component."""
        domain = entity_id.split(".", 1)[0]
        if domain not in _WIDGET_DOMAINS:
            return False


        if new_state is None or new_state.state in _WIDGET_UNSETTLED:
            return False
        if old_state is None or old_state.state in _WIDGET_UNSETTLED:
            return False
        return old_state.state != new_state.state

    @callback
    def _mark_widgets_dirty(self) -> None:
        """CasaSmart runtime component."""
        if self._widget_flush_cancel is not None:
            return
        self._widget_flush_cancel = async_call_later(
            self._hass, _WIDGET_PUSH_COALESCE_SECONDS, self._flush_widget_refresh
        )

    @callback
    def _flush_widget_refresh(self, _now: Any) -> None:
        """CasaSmart runtime component."""
        self._widget_flush_cancel = None
        data = {"type": PUSH_TYPE_UPDATE_WIDGETS, "silent": "1"}
        self._schedule_dispatch(self._dispatch(data, PRIORITY_NORMAL))

    @staticmethod
    def _is_real_lock_transition(old_state: Any, new_state: Any) -> bool:
        """CasaSmart runtime component."""
        if new_state is None or new_state.state not in _LOCK_SETTLED:
            return False
        if old_state is None or old_state.state in _LOCK_FLAP_STATES:
            return False
        return old_state.state != new_state.state



    def _build_security_payload(self, alarm_event: dict[str, Any]) -> dict[str, str]:
        """CasaSmart runtime component."""
        life_safety = bool(alarm_event.get("life_safety"))
        entity_id = alarm_event.get("entity_id")
        zone = alarm_event.get("zone")
        name = self._friendly_name(entity_id) or (zone if isinstance(zone, str) else None)
        title = "Life-safety alarm" if life_safety else "Security alarm"
        body = f"{name} triggered the alarm" if name else "The alarm was triggered"
        data = {"type": PUSH_TYPE_SECURITY, "title": title, "body": body}
        if life_safety:


            data["life_safety"] = "1"
        if isinstance(entity_id, str) and entity_id:
            data["entity_id"] = entity_id
        return data

    def _build_lock_payload(
        self, entity_id: str, new_state: Any
    ) -> dict[str, str]:
        """CasaSmart runtime component."""
        locked = new_state.state == STATE_LOCKED
        name = self._friendly_name(entity_id) or entity_id
        action = "locked" if locked else "unlocked"
        return {
            "type": PUSH_TYPE_LOCK,
            "title": f"Door {action}",
            "body": f"{name} was {action}",
            "entity_id": entity_id,
        }

    def _friendly_name(self, entity_id: Any) -> Optional[str]:
        """CasaSmart runtime component."""
        if not isinstance(entity_id, str) or not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        name = state.attributes.get("friendly_name")
        return name if isinstance(name, str) and name else None



    async def async_send(self, data: dict[str, str], priority: str) -> None:
        """CasaSmart runtime component."""
        await self._dispatch(data, priority)

    async def async_send_device_paired(
        self, name: str, role: str, device_id: str
    ) -> None:
        """CasaSmart runtime component."""
        await self._dispatch(
            {
                "type": PUSH_TYPE_DEVICE_PAIRED,
                "title": "New device paired",
                "body": f"{name} ({role})",
                "device_id": device_id,
            },
            PRIORITY_NORMAL,
        )

    async def _dispatch(self, data: dict[str, str], priority: str) -> None:
        """CasaSmart runtime component."""
        try:
            await self._dispatch_inner(data, priority)
        except Exception:
            _LOGGER.exception("Push dispatch failed for a %s event", data.get("type"))

    async def _dispatch_inner(self, data: dict[str, str], priority: str) -> None:
        if not self._active:
            return
        try:
            tokens = await self._hass.async_add_executor_job(
                self._push_store.get_all_tokens
            )
        except Exception:
            _LOGGER.exception("Push dispatch: reading device tokens failed")
            return





        owner_only = (
            data.get("type") in _OWNER_ONLY_TYPES
            and data.get("life_safety") != "1"
        )
        engine = None
        if owner_only:
            from .auth_api import get_engine

            engine = get_engine(self._hass)
        device_tokens = [
            rec["fcm_token"]
            for dev_id, rec in tokens.items()
            if isinstance(rec.get("fcm_token"), str)
            and rec["fcm_token"]
            and (not owner_only or engine is None or engine.is_owner_device(dev_id))
        ]
        if not device_tokens:
            _LOGGER.debug(
                "Push dispatch: no registered tokens — %s push dropped",
                data.get("type"),
            )
            return

        body = self._build_request(device_tokens, data, priority)
        if not self._active:
            return
        await self._send(body)

    def _build_request(
        self, device_tokens: list[str], data: dict[str, str], priority: str
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        signed = {
            "hub_id": self._hub_id,
            "timestamp": int(self._clock()),
            "nonce": secrets.token_hex(_NONCE_BYTES),
            "priority": priority,
            "payloads": [
                {"device_token": token, "data": data} for token in device_tokens
            ],
        }
        canonical = json.dumps(
            signed, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        )
        signature = b64encode(self._signer.sign(canonical.encode("utf-8"))).decode(
            "ascii"
        )
        return {**signed, "signature": signature}

    async def _send(self, body: dict[str, Any]) -> None:
        """CasaSmart runtime component."""
        if not self._active:
            return
        timeout = aiohttp.ClientTimeout(total=PUSH_RELAY_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                self._relay_url, json=body, timeout=timeout
            ) as resp:
                status = resp.status
                payload = await self._read_json(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Push relay unreachable (%s): %s", self._relay_url, err)
            return

        if status != 200:
            _LOGGER.warning("Push relay rejected batch: HTTP %s %s", status, payload)
            return
        await self._cleanup_dead_tokens(payload)

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> Any:
        """CasaSmart runtime component."""
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError):
            return None

    async def _cleanup_dead_tokens(self, payload: Any) -> None:
        """CasaSmart runtime component."""
        if not isinstance(payload, dict):
            return
        errors = payload.get("errors")
        if not isinstance(errors, list):
            return
        dead = {
            err["device_token"]
            for err in errors
            if isinstance(err, dict)
            and err.get("action") == "remove_token"
            and isinstance(err.get("device_token"), str)
        }
        if not dead:
            return
        try:
            removed = await self._hass.async_add_executor_job(self._remove_tokens, dead)
        except Exception:
            _LOGGER.exception("Push dispatch: dead-token cleanup failed")
            return
        if removed:
            _LOGGER.info(
                "Push dispatch: removed %d stale token(s) flagged by the relay",
                removed,
            )

    def _remove_tokens(self, dead: set[str]) -> int:
        """CasaSmart runtime component."""
        removed = 0
        for device_id, rec in self._push_store.get_all_tokens().items():
            if rec.get("fcm_token") in dead and self._push_store.unregister(device_id):
                removed += 1
        return removed





TANK_LOW_CHECK_UTC_HOUR = 15

TANK_OFFLINE_TIMEOUT_SECONDS = 20 * 60


TANK_OFFLINE_POLL = timedelta(minutes=5)

_SECONDS_PER_DAY = 86400
_AST_OFFSET_SECONDS = 3 * 3600


class TankPushMonitor:
    """CasaSmart runtime component."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        tanks: "TankEngine",
        notifier: PushDispatcher,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._hass = hass
        self._tanks = tanks
        self._notifier = notifier
        self._clock = clock
        self._unsub_daily: Optional[Callable[[], None]] = None
        self._unsub_offline: Optional[Callable[[], None]] = None


        self._low_pushed_day: dict[str, int] = {}
        self._offline_pushed_day: dict[str, int] = {}



    @callback
    def async_start(self) -> None:
        """CasaSmart runtime component."""
        from homeassistant.helpers.event import (
            async_track_time_interval,
            async_track_utc_time_change,
        )

        self._unsub_daily = async_track_utc_time_change(
            self._hass,
            self._handle_daily_check,
            hour=TANK_LOW_CHECK_UTC_HOUR,
            minute=0,
            second=0,
        )
        self._unsub_offline = async_track_time_interval(
            self._hass, self._handle_offline_check, TANK_OFFLINE_POLL
        )
        _LOGGER.info(
            "Tank push monitor started (low-water sweep 18:00 AST, "
            "offline watchdog every %s)",
            TANK_OFFLINE_POLL,
        )

    @callback
    def async_stop(self) -> None:
        """CasaSmart runtime component."""
        if self._unsub_daily is not None:
            self._unsub_daily()
            self._unsub_daily = None
        if self._unsub_offline is not None:
            self._unsub_offline()
            self._unsub_offline = None



    async def _handle_daily_check(self, _now: Any = None) -> None:
        await self.async_check_low_water()

    async def _handle_offline_check(self, _now: Any = None) -> None:
        await self.async_check_offline()



    async def async_check_low_water(self) -> None:
        """CasaSmart runtime component."""
        now = self._clock()
        day = self._ast_day(now)
        devices = await self._list_devices()
        for device in devices:
            device_id = device.get("device_id")
            if not device_id or not device.get("is_calibrated"):
                continue
            if self._low_pushed_day.get(device_id) == day:
                continue
            last = device.get("last_reading")



            if not last or now - last.get("t", 0) >= TANK_OFFLINE_TIMEOUT_SECONDS:
                continue
            try:
                status = await self._hass.async_add_executor_job(
                    self._tanks.status, device_id
                )
            except Exception:
                _LOGGER.exception("Tank %s: status read failed", device_id)
                continue
            if status.get("percent") is None or not status.get("is_low"):
                continue
            self._low_pushed_day[device_id] = day
            await self._emit_low(device, status)

    async def async_check_offline(self) -> None:
        """CasaSmart runtime component."""
        now = self._clock()
        day = self._ast_day(now)
        devices = await self._list_devices()
        for device in devices:
            device_id = device.get("device_id")
            if not device_id:
                continue
            last = device.get("last_reading")


            if not last:
                continue
            if now - last.get("t", 0) < TANK_OFFLINE_TIMEOUT_SECONDS:
                continue
            if self._offline_pushed_day.get(device_id) == day:
                continue
            self._offline_pushed_day[device_id] = day
            await self._emit_offline(device, last)



    async def _emit_low(
        self, device: dict[str, Any], status: dict[str, Any]
    ) -> None:
        device_id = device["device_id"]
        name = device.get("name") or "Water tank"
        percent = int(round(status["percent"]))
        self._hass.bus.async_fire(
            EVENT_TANK_LOW,
            {
                "device_id": device_id,
                "name": name,
                "percent": status["percent"],
                "low_percent": status.get("low_percent"),
            },
        )
        await self._notifier.async_send(
            {
                "type": PUSH_TYPE_TANK_LOW,
                "title": "Water tank low",
                "body": f"{name} level is low ({percent}%)",
                "device_id": device_id,
                "percent": str(percent),
            },
            PRIORITY_NORMAL,
        )

    async def _emit_offline(
        self, device: dict[str, Any], last: dict[str, Any]
    ) -> None:
        device_id = device["device_id"]
        name = device.get("name") or "Water tank"
        self._hass.bus.async_fire(
            EVENT_TANK_OFFLINE,
            {
                "device_id": device_id,
                "name": name,
                "last_reading_at": last.get("t"),
            },
        )
        await self._notifier.async_send(
            {
                "type": PUSH_TYPE_TANK_OFFLINE,
                "title": "Water tank offline",
                "body": f"{name} — no readings for 20+ minutes",
                "device_id": device_id,
            },
            PRIORITY_NORMAL,
        )



    async def _list_devices(self) -> list[dict[str, Any]]:
        try:
            return await self._hass.async_add_executor_job(self._tanks.list_devices)
        except Exception:
            _LOGGER.exception("Tank monitor: listing devices failed")
            return []

    @staticmethod
    def _ast_day(now: float) -> int:
        """CasaSmart runtime component."""
        return int((now + _AST_OFFSET_SECONDS) // _SECONDS_PER_DAY)
