"""Push notifications, signed by the hub and delivered through the push relay.

``PushDispatcher`` turns hub events into pushes: alarm triggers
(``casasmart_alarm_triggered``), settled lock and unlock changes, a newly
paired device, HQ reminders (``push_api``), and a silent "refresh your
widgets" nudge when a control entity's state settles. ``TankPushMonitor``
adds the timer-driven water-tank alerts.

Each push is one batch to the relay's push endpoint: the same payload for
every target device's FCM token, signed with the hub's Ed25519 push key
(``push_crypto``). The relay checks the signature against the public key the
hub registered (``relay_registration``) and delivers each payload to its
token. Payloads are plain text, protected in transit by HTTPS; the hub does
not encrypt them.

Most alert types go to the owner's (admin) devices only. A life-safety alarm
goes to every registered device, and so do alarm and lock alerts when device
roles can't be read.

``__init__.py`` creates both objects once a relay is configured and the TLS
and push identities load; without them the hub runs without push.
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

# The ``type`` field of a push payload; the apps route on it. The tank and
# widget types live in const.py.
PUSH_TYPE_SECURITY = "security"
PUSH_TYPE_LOCK = "lock"
PUSH_TYPE_DEVICE_PAIRED = "device_paired"
PUSH_TYPE_HQ_REMINDER = "hq_reminder"

# Sent only to the owner's (admin) devices. A life-safety alarm overrides this
# for its own push: smoke, gas, CO or a leak must reach everyone in the house.
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

# When device roles can't be resolved (the auth engine isn't loaded), owner-only
# pushes fail CLOSED — except alarm and lock alerts, which fail OPEN to every
# registered device: a missed break-in or door alert is worse than one extra
# notification on a family member's phone.
_FAIL_OPEN_WITHOUT_ROLES = frozenset({PUSH_TYPE_SECURITY, PUSH_TYPE_LOCK})

# Batch priority sent to the relay; only alarm triggers are critical.
PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"

# --- Event filters ----------------------------------------------------------

STATE_LOCKED = "locked"
STATE_UNLOCKED = "unlocked"

_LOCK_PREFIX = "lock."
_LOCK_SETTLED = frozenset({STATE_LOCKED, STATE_UNLOCKED})
_LOCK_FLAP_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

# Domains whose state a home-screen widget shows.
_WIDGET_DOMAINS = frozenset(
    {"light", "switch", "input_boolean", "lock", "cover", "climate", "fan"}
)

# Widget refreshes are coalesced: the first change arms one push this many
# seconds later, and changes inside the window ride along with it.
_WIDGET_PUSH_COALESCE_SECONDS = 15.0

_WIDGET_UNSETTLED = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

# Random bytes of the per-batch nonce (hex-encoded in the request). With the
# timestamp it lets the relay refuse a replayed batch.
_NONCE_BYTES = 32


class PushDispatcher:
    """Sends hub events to the push relay as signed batches.

    ``async_start`` subscribes to alarm triggers and state changes;
    ``async_stop`` drops the subscriptions, the pending widget flush and any
    send still in flight. Sending never raises: every send path returns a
    small result such as ``{"delivery": "relay_accepted"}`` or
    ``{"delivery": "failed", "reason": ...}``, because a push is best effort
    and must never fail the code that triggered it.
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
        """Run a send as a tracked task, so ``async_stop`` can cancel it."""
        task = self._hass.async_create_task(coro)
        if isinstance(task, asyncio.Task):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @callback
    def _on_alarm_triggered(self, event: Event) -> None:
        """A critical push for every ``casasmart_alarm_triggered``."""
        data = self._build_security_payload(event.data or {})
        self._schedule_dispatch(self._dispatch(data, PRIORITY_CRITICAL))

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Lock pushes and widget refreshes, from HA's state changes."""
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
        """True for a settled control-domain state edge a widget would render."""
        domain = entity_id.split(".", 1)[0]
        if domain not in _WIDGET_DOMAINS:
            return False
        # Need a real new value and a real prior value that actually differs —
        # ignore attribute-only churn and flaps in/out of unavailable/unknown.
        if new_state is None or new_state.state in _WIDGET_UNSETTLED:
            return False
        if old_state is None or old_state.state in _WIDGET_UNSETTLED:
            return False
        return old_state.state != new_state.state

    @callback
    def _mark_widgets_dirty(self) -> None:
        """Arm one coalesced widget-refresh flush; absorb changes within it."""
        if self._widget_flush_cancel is not None:
            return  # a flush is already scheduled for this window
        self._widget_flush_cancel = async_call_later(
            self._hass, _WIDGET_PUSH_COALESCE_SECONDS, self._flush_widget_refresh
        )

    @callback
    def _flush_widget_refresh(self, _now: Any) -> None:
        """Timer callback: send this window's one silent widget refresh."""
        self._widget_flush_cancel = None
        data = {"type": PUSH_TYPE_UPDATE_WIDGETS, "silent": "1"}
        self._schedule_dispatch(self._dispatch(data, PRIORITY_NORMAL))

    @staticmethod
    def _is_real_lock_transition(old_state: Any, new_state: Any) -> bool:
        """True only for a settled ``locked``<->``unlocked`` change.

        The new state must be a settled lock state; the old state must be a real
        prior state (not ``unavailable``/``unknown`` and not ``None``); and they
        must differ. This catches the genuine edge even when the lock reported an
        intermediate ``locking``/``unlocking`` first, while ignoring flaps in and
        out of ``unavailable``.
        """
        if new_state is None or new_state.state not in _LOCK_SETTLED:
            return False
        if old_state is None or old_state.state in _LOCK_FLAP_STATES:
            return False
        return old_state.state != new_state.state

    def _build_security_payload(self, alarm_event: dict[str, Any]) -> dict[str, str]:
        """Plaintext security notification from an alarm event dict."""
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
            # House-wide audience flag: a life-safety alarm (smoke, gas, CO, a
            # leak) must reach everyone, not just the owner (_dispatch_inner).
            data["life_safety"] = "1"
        if isinstance(entity_id, str) and entity_id:
            data["entity_id"] = entity_id
        return data

    def _build_lock_payload(self, entity_id: str, new_state: Any) -> dict[str, str]:
        """Plaintext lock notification for a settled lock transition."""
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
        """Tell the owner a new device was paired ("New device paired").

        The enroll view calls this once a new device has enrolled (a phone
        pairing again with the same key doesn't count), so a member code
        redeemed on the LAN or, with ``remote_pairing_enabled``, from anywhere
        always reaches the owner. Owner-only, like the alarm, lock and tank
        alerts. It is a notice, not an approval step. Never raises.
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
        """``_dispatch_inner`` behind a last-resort guard: a push never raises."""
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
                        "Push dispatch: device roles unavailable — sending the "
                        "%s alert to every registered device",
                        data.get("type"),
                    )
                    owner_only = False
                else:
                    _LOGGER.warning(
                        "Push dispatch: device roles unavailable — %s push "
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
                "Push dispatch: no registered tokens — %s push dropped",
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

        The canonical JSON signed here MUST match what the relay re-derives:
        sorted keys, no whitespace, raw UTF-8 (``ensure_ascii=False``), integer
        timestamp. ``signature`` is excluded from the signed bytes and added
        afterwards.
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
        """Drop every token the relay flagged ``remove_token``.

        The relay flags a token its push service reports as no longer
        registered, for example because the app was uninstalled.
        """
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
        """Executor: unregister every device whose token the relay rejected."""
        removed = 0
        for device_id, rec in self._push_store.get_all_tokens().items():
            if rec.get("fcm_token") in dead and self._push_store.unregister(device_id):
                removed += 1
        return removed


# --- Water-tank alerts -----------------------------------------------------

# Local wall-clock hour (HA's configured time zone) of the daily low-water sweep.
TANK_LOW_CHECK_LOCAL_HOUR = 18

# A tank that has reported before and then sends nothing for this long is
# offline. Sensors post every 5 minutes by default.
TANK_OFFLINE_TIMEOUT_SECONDS = 20 * 60

# How often the offline watchdog runs.
TANK_OFFLINE_POLL = timedelta(minutes=5)


class TankPushMonitor:
    """Water-tank low-level and offline alerts.

    Unlike the alarm and lock pushes this is timer-driven, not event-driven:
    tank readings arrive by REST every 5 minutes, and "low at 6pm" or "silent
    for 20 minutes" are questions about time, not state changes. Two timers:

    - a daily 18:00 sweep in Home Assistant's time zone: every calibrated tank
      whose latest reading is below its ``low_percent`` gets one push;
    - a 5-minute watchdog: one push when a tank that was reporting has gone
      silent for 20 minutes or more.

    Each is limited to one push per tank per local calendar day (so the
    watchdog doesn't repeat every 5 minutes). Each alert first fires its HA
    event (``casasmart_tank_low`` / ``casasmart_tank_offline``) for
    automations, then sends the owner push through
    ``PushDispatcher.async_send``.

    The checks (``async_check_low_water`` / ``async_check_offline``) take
    "now" from an injectable ``clock`` and read the engine through the
    executor, so they can be tested without HA timers or a relay.
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
        # device_id -> local day ordinal of the last push, one map per channel so a
        # low-water push and an offline push don't suppress each other.
        self._low_pushed_day: dict[str, int] = {}
        self._offline_pushed_day: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------------

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

    # -- timer adapters --------------------------------------------------------

    async def _handle_daily_check(self, _now: Any = None) -> None:
        await self.async_check_low_water()

    async def _handle_offline_check(self, _now: Any = None) -> None:
        await self.async_check_offline()

    # -- checks (testable; clock-driven) ---------------------------------------

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
            # A dead sensor must not raise a "low" alert off a stale reading —
            # that's the offline watchdog's job. Skip when the latest reading is
            # already past the offline threshold.
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
        """Push once for each previously-reporting tank now silent 20+ min."""
        now = self._clock()
        day = self._local_day(now)
        devices = await self._list_devices()
        for device in devices:
            device_id = device.get("device_id")
            if not device_id:
                continue
            last = device.get("last_reading")
            # "previously reporting": a tank with no reading at all (freshly
            # provisioned, never POSTed) is not offline, it's pending.
            if not last:
                continue
            if now - last.get("t", 0) < TANK_OFFLINE_TIMEOUT_SECONDS:
                continue
            if self._offline_pushed_day.get(device_id) == day:
                continue
            self._offline_pushed_day[device_id] = day
            await self._emit_offline(device, last)

    # -- emit ------------------------------------------------------------------

    async def _emit_low(self, device: dict[str, Any], status: dict[str, Any]) -> None:
        """Fire ``casasmart_tank_low``, then push to the owner."""
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
        """Fire ``casasmart_tank_offline``, then push to the owner."""
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

    # -- internals -------------------------------------------------------------

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
        """The local calendar-day ordinal for the once-per-day dedup."""
        return datetime.fromtimestamp(now, self._time_zone()).date().toordinal()
