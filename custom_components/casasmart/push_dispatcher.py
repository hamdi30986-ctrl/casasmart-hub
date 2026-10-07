"""Push notifications, signed by the hub and delivered by the push relay.

PushDispatcher turns alarm triggers, settled lock changes, newly paired
devices and HQ reminders into pushes, and sends a silent widget refresh when
a control entity settles. TankPushMonitor adds the timed water-tank alerts.
Each push is one batch for every target device's FCM token, signed with the
hub's Ed25519 push key (push_crypto). Payloads are plain text over HTTPS.
Setup creates both objects only when a relay is configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from base64 import b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# --- Push types and audiences -----------------------------------------------

# Payload "type" values; the apps route on them. Tank and widget types are in
# const.py.
PUSH_TYPE_SECURITY = "security"
PUSH_TYPE_LOCK = "lock"
PUSH_TYPE_DEVICE_PAIRED = "device_paired"
PUSH_TYPE_HQ_REMINDER = "hq_reminder"

# Sent to the owner's (admin) devices only, except a life-safety alarm (smoke,
# gas, CO, a leak), which goes to everyone.
_OWNER_ONLY_TYPES = frozenset(
    {
        PUSH_TYPE_SECURITY,
        PUSH_TYPE_LOCK,
        PUSH_TYPE_TANK_LOW,
        PUSH_TYPE_TANK_OFFLINE,
        PUSH_TYPE_DEVICE_PAIRED,
        PUSH_TYPE_HQ_REMINDER,
    }
)

# Without device roles (auth engine not loaded) owner-only pushes are withheld,
# except alarm and lock alerts: a missed break-in is worse than an extra alert.
_FAIL_OPEN_WITHOUT_ROLES = frozenset({PUSH_TYPE_SECURITY, PUSH_TYPE_LOCK})

# Batch priority sent to the relay; only alarm triggers are critical.
PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"

# --- Event filters ----------------------------------------------------------

STATE_LOCKED = "locked"
STATE_UNLOCKED = "unlocked"

_LOCK_PREFIX = "lock."
_LOCK_SETTLED = frozenset({STATE_LOCKED, STATE_UNLOCKED})
# States an entity flaps through; a change to or from one isn't an event.
_UNSETTLED = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

# Domains whose state a home-screen widget shows.
_WIDGET_DOMAINS = frozenset(
    {"light", "switch", "input_boolean", "lock", "cover", "climate", "fan"}
)

# The first widget change arms one refresh this many seconds later; changes
# inside the window share it.
_WIDGET_PUSH_COALESCE_SECONDS = 15.0

# With the timestamp, the per-batch nonce lets the relay refuse a replay.
_NONCE_BYTES = 32


class PushDispatcher:
    """Sends hub events to the push relay as signed batches.

    A push is best effort, so sending never raises. Each send returns a small
    result such as {"delivery": "relay_accepted"} or {"delivery": "failed",
    "reason": ...}.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        push_store: PushTokenStore,
        signer: PushSigner,
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
        self._unsub_alarm: Callable[[], None] | None = None
        self._unsub_state: Callable[[], None] | None = None

        self._widget_flush_cancel: Callable[[], None] | None = None
        self._active = False
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def relay_url(self) -> str:
        """The relay push endpoint this dispatcher posts to."""
        return self._relay_url

    @callback
    def async_start(self) -> None:
        """Subscribe to alarm triggers and state changes."""
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
        """Unsubscribe and cancel pending work; safe to call more than once."""
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
        """Run a send as a tracked task, so async_stop can cancel it."""
        task = self._hass.async_create_task(coro)
        if isinstance(task, asyncio.Task):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @callback
    def _on_alarm_triggered(self, event: Event) -> None:
        """Send a critical push for an alarm trigger."""
        data = self._build_security_payload(event.data or {})
        self._schedule_dispatch(self._dispatch(data, PRIORITY_CRITICAL))

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Send lock pushes and mark widgets dirty from HA state changes."""
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
        """True when a widget-domain entity moves between two known states."""
        domain = entity_id.split(".", 1)[0]
        if domain not in _WIDGET_DOMAINS:
            return False
        if new_state is None or new_state.state in _UNSETTLED:
            return False
        if old_state is None or old_state.state in _UNSETTLED:
            return False
        return old_state.state != new_state.state

    @callback
    def _mark_widgets_dirty(self) -> None:
        """Schedule a widget refresh unless one is already pending."""
        if self._widget_flush_cancel is not None:
            return
        self._widget_flush_cancel = async_call_later(
            self._hass, _WIDGET_PUSH_COALESCE_SECONDS, self._flush_widget_refresh
        )

    @callback
    def _flush_widget_refresh(self, _now: Any) -> None:
        """Send the window's silent widget refresh."""
        self._widget_flush_cancel = None
        data = {"type": PUSH_TYPE_UPDATE_WIDGETS, "silent": "1"}
        self._schedule_dispatch(self._dispatch(data, PRIORITY_NORMAL))

    @staticmethod
    def _is_real_lock_transition(old_state: Any, new_state: Any) -> bool:
        """True when a lock settles in a new state from a known prior state.

        A change by way of locking or unlocking counts; one from unavailable
        or unknown doesn't.
        """
        if new_state is None or new_state.state not in _LOCK_SETTLED:
            return False
        if old_state is None or old_state.state in _UNSETTLED:
            return False
        return old_state.state != new_state.state

    def _build_security_payload(self, alarm_event: dict[str, Any]) -> dict[str, str]:
        """The security push for an alarm event."""
        life_safety = bool(alarm_event.get("life_safety"))
        entity_id = alarm_event.get("entity_id")
        zone = alarm_event.get("zone")
        name = self._friendly_name(entity_id) or (
            zone if isinstance(zone, str) else None
        )
        title = "Life-safety alarm" if life_safety else "Security alarm"
        body = f"{name} triggered the alarm" if name else "The alarm was triggered"
        data = {"type": PUSH_TYPE_SECURITY, "title": title, "body": body}
        if life_safety:
            # Sends this owner-only type to every device (_dispatch_inner).
            data["life_safety"] = "1"
        if isinstance(entity_id, str) and entity_id:
            data["entity_id"] = entity_id
        return data

    def _build_lock_payload(self, entity_id: str, new_state: Any) -> dict[str, str]:
        """The lock push for a settled lock change."""
        locked = new_state.state == STATE_LOCKED
        name = self._friendly_name(entity_id) or entity_id
        action = "locked" if locked else "unlocked"
        return {
            "type": PUSH_TYPE_LOCK,
            "title": f"Door {action}",
            "body": f"{name} was {action}",
            "entity_id": entity_id,
        }

    def _friendly_name(self, entity_id: Any) -> str | None:
        """The entity's friendly name from the live state, or None."""
        if not isinstance(entity_id, str) or not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        name = state.attributes.get("friendly_name")
        return name if isinstance(name, str) and name else None

    async def async_send(self, data: dict[str, str], priority: str) -> dict[str, str]:
        """Send one push and return its delivery result; never raises.

        The tank monitor and the HQ notification view send through here.
        """
        return await self._dispatch(data, priority)

    async def async_send_device_paired(
        self, name: str, role: str, device_id: str
    ) -> None:
        """Tell the owner a new device was paired; never raises.

        The enroll view calls this for each new device, but not for a phone
        pairing again with the same key. The device is already paired.
        """
        await self._dispatch(
            {
                "type": PUSH_TYPE_DEVICE_PAIRED,
                "title": "New device paired",
                "body": f"{name} ({role})",
                "device_id": device_id,
            },
            PRIORITY_NORMAL,
        )

    async def _dispatch(self, data: dict[str, str], priority: str) -> dict[str, str]:
        """Run _dispatch_inner behind a last-resort guard, so a push never raises."""
        try:
            return await self._dispatch_inner(data, priority)
        except Exception:
            _LOGGER.exception("Push dispatch failed for a %s event", data.get("type"))
            return {"delivery": "failed", "reason": "dispatcher_error"}

    async def _dispatch_inner(
        self, data: dict[str, str], priority: str
    ) -> dict[str, str]:
        """Pick the audience's tokens, sign the batch and post it."""
        if not self._active:
            return {"delivery": "unavailable", "reason": "dispatcher_inactive"}
        try:
            tokens = await self._hass.async_add_executor_job(
                self._push_store.get_all_tokens
            )
        except Exception:
            _LOGGER.exception("Push dispatch: reading device tokens failed")
            return {"delivery": "failed", "reason": "token_store_error"}

        owner_only = (
            data.get("type") in _OWNER_ONLY_TYPES and data.get("life_safety") != "1"
        )
        engine = None
        if owner_only:
            from .auth_api import get_engine

            engine = get_engine(self._hass)
            if engine is None:
                if data.get("type") in _FAIL_OPEN_WITHOUT_ROLES:
                    _LOGGER.warning(
                        "Push dispatch: device roles unavailable; sending the "
                        "%s alert to every registered device",
                        data.get("type"),
                    )
                    owner_only = False
                else:
                    _LOGGER.warning(
                        "Push dispatch: device roles unavailable; %s push "
                        "withheld (owner-only)",
                        data.get("type"),
                    )
        device_tokens = [
            rec["fcm_token"]
            for dev_id, rec in tokens.items()
            if isinstance(rec.get("fcm_token"), str)
            and rec["fcm_token"]
            and (
                not owner_only
                or (engine is not None and engine.is_owner_device(dev_id))
            )
        ]
        if not device_tokens:
            _LOGGER.debug(
                "Push dispatch: no registered tokens; %s push dropped",
                data.get("type"),
            )
            return {"delivery": "no_registered_tokens"}

        body = self._build_request(device_tokens, data, priority)
        # The dispatcher may have been stopped while the tokens were read.
        if not self._active:
            return {"delivery": "unavailable", "reason": "dispatcher_inactive"}
        return await self._send(body)

    def _build_request(
        self, device_tokens: list[str], data: dict[str, str], priority: str
    ) -> dict[str, Any]:
        """Build the signed relay request body.

        The relay rebuilds the signed bytes from the other fields, so their
        canonical form (sorted keys, no whitespace, raw UTF-8, integer
        timestamp) must not change.
        """
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

    async def _send(self, body: dict[str, Any]) -> dict[str, str]:
        """POST the batch; on success drop any token the relay reports dead."""
        if not self._active:
            return {"delivery": "unavailable", "reason": "dispatcher_inactive"}
        timeout = aiohttp.ClientTimeout(total=PUSH_RELAY_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                self._relay_url, json=body, timeout=timeout
            ) as resp:
                status = resp.status
                payload = await self._read_json(resp)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Push relay unreachable (%s): %s", self._relay_url, err)
            return {"delivery": "failed", "reason": "relay_unreachable"}

        if status != 200:
            _LOGGER.warning("Push relay rejected batch: HTTP %s %s", status, payload)
            return {"delivery": "failed", "reason": "relay_rejected"}
        await self._cleanup_dead_tokens(payload)
        return {"delivery": "relay_accepted"}

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> Any:
        """Best-effort JSON parse of a relay response, or None."""
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError):
            return None

    async def _cleanup_dead_tokens(self, payload: Any) -> None:
        """Drop the tokens the relay flags remove_token, such as an uninstalled app's."""
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
        """Unregister each device whose token the relay rejected (executor)."""
        removed = 0
        for device_id, rec in self._push_store.get_all_tokens().items():
            if rec.get("fcm_token") in dead and self._push_store.unregister(device_id):
                removed += 1
        return removed


# --- Water-tank alerts -----------------------------------------------------

# Local wall-clock hour (HA's configured time zone) of the daily low-water sweep.
TANK_LOW_CHECK_LOCAL_HOUR = 18

# A tank that has reported before is offline after this long without a
# reading. Sensors post every 5 minutes by default.
TANK_OFFLINE_TIMEOUT_SECONDS = 20 * 60

# How often the offline watchdog runs.
TANK_OFFLINE_POLL = timedelta(minutes=5)


class TankPushMonitor:
    """Water-tank low-level and offline alerts.

    These run on timers, since "low at 18:00" and "silent for 20 minutes" are
    questions about time. A daily sweep at 18:00 (HA's time zone) checks every
    calibrated tank against its low_percent, and a 5-minute watchdog looks for
    tanks that stopped reporting. Each alert fires its HA event, then pushes
    to the owner, at most once per tank per local day.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        tanks: TankEngine,
        notifier: PushDispatcher,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._hass = hass
        self._tanks = tanks
        self._notifier = notifier
        self._clock = clock
        self._unsub_daily: Callable[[], None] | None = None
        self._unsub_offline: Callable[[], None] | None = None
        # device_id -> local day of the last push. Separate maps, so a low-water
        # push doesn't suppress an offline push or the reverse.
        self._low_pushed_day: dict[str, int] = {}
        self._offline_pushed_day: dict[str, int] = {}

    @callback
    def async_start(self) -> None:
        """Start the daily low-water sweep and the offline watchdog."""
        # Imported at call time so tests can patch these helpers on
        # homeassistant.helpers.event.
        from homeassistant.helpers.event import (
            async_track_time_change,
            async_track_time_interval,
        )

        # async_track_time_change follows HA's configured time zone.
        self._unsub_daily = async_track_time_change(
            self._hass,
            self._handle_daily_check,
            hour=TANK_LOW_CHECK_LOCAL_HOUR,
            minute=0,
            second=0,
        )
        self._unsub_offline = async_track_time_interval(
            self._hass, self._handle_offline_check, TANK_OFFLINE_POLL
        )
        _LOGGER.info(
            "Tank push monitor started (low-water sweep %02d:00 %s, "
            "offline watchdog every %s)",
            TANK_LOW_CHECK_LOCAL_HOUR,
            self._time_zone(),
            TANK_OFFLINE_POLL,
        )

    @callback
    def async_stop(self) -> None:
        """Cancel both timers; safe to call more than once."""
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
        """Push once for each calibrated tank currently below its threshold."""
        now = self._clock()
        day = self._local_day(now)
        devices = await self._list_devices()
        for device in devices:
            device_id = device.get("device_id")
            if not device_id or not device.get("is_calibrated"):
                continue
            if self._low_pushed_day.get(device_id) == day:
                continue
            last = device.get("last_reading")
            # A stale reading is for the offline watchdog to report.
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
        """Push once for each tank that has reported but is now silent."""
        now = self._clock()
        day = self._local_day(now)
        devices = await self._list_devices()
        for device in devices:
            device_id = device.get("device_id")
            if not device_id:
                continue
            last = device.get("last_reading")
            # A tank that has never reported is still waiting for its first reading.
            if not last:
                continue
            if now - last.get("t", 0) < TANK_OFFLINE_TIMEOUT_SECONDS:
                continue
            if self._offline_pushed_day.get(device_id) == day:
                continue
            self._offline_pushed_day[device_id] = day
            await self._emit_offline(device, last)

    async def _emit_low(self, device: dict[str, Any], status: dict[str, Any]) -> None:
        """Fire casasmart_tank_low, then push to the owner."""
        device_id = device["device_id"]
        name = device.get("name") or "Water tank"
        percent = round(status["percent"])
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

    async def _emit_offline(self, device: dict[str, Any], last: dict[str, Any]) -> None:
        """Fire casasmart_tank_offline, then push to the owner."""
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
                "body": f"{name}: no readings for 20+ minutes",
                "device_id": device_id,
            },
            PRIORITY_NORMAL,
        )

    async def _list_devices(self) -> list[dict[str, Any]]:
        """Every tank device, or [] (logged) if the engine read fails."""
        try:
            return await self._hass.async_add_executor_job(self._tanks.list_devices)
        except Exception:
            _LOGGER.exception("Tank monitor: listing devices failed")
            return []

    def _time_zone(self) -> tzinfo:
        """HA's configured time zone (UTC if it is unset or unknown)."""
        name = getattr(getattr(self._hass, "config", None), "time_zone", None)
        if isinstance(name, str) and name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return UTC

    def _local_day(self, now: float) -> int:
        """The local calendar day, for the once-a-day limit."""
        return datetime.fromtimestamp(now, self._time_zone()).date().toordinal()
