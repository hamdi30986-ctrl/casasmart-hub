"""Hub push dispatcher (B8 Piece 4) — alarm + lock notifications.

This is the leg the ``alarm_adapter`` deliberately left stubbed: turning hub
events into push notifications. It subscribes to two bus signals, builds a
plaintext notification payload, signs the batch with the hub's Ed25519
push-identity key (``push_crypto.py``), and POSTs it to the relay, which
authenticates the signature and fans out to FCM.

What it listens to:

- ``EVENT_ALARM_TRIGGERED`` — an armed zone or a life-safety sensor actually
  tripped. Pushed on the **critical** relay bucket (its own rate-limit quota).
- ``state_changed`` for ``lock.*`` entities — pushed **normal**, but ONLY on a
  real ``locked``<->``unlocked`` transition. A lock flapping through
  ``unavailable``/``unknown`` (or an intermediate ``locking``/``unlocking``
  that never settles) never fires; only the settled edge does.

Piece 4b adds ``TankPushMonitor`` alongside the dispatcher: a timer-driven (not
event-driven) watcher that pushes water-tank low-level and offline alerts. It
does not subscribe to the bus — it polls the hub-authoritative ``TankEngine`` on
a schedule (a daily 18:00-AST low sweep + a 5-minute offline watchdog) and
dispatches through the dispatcher's public ``async_send``, so both legs sign and
relay identically. Each alert is also fired on the HA bus (EVENT_TANK_LOW /
EVENT_TANK_OFFLINE) as the installer-automation hook.

The wire payload is plaintext (the relay reads ``data.title``/``data.body`` to
build the visible alert), so the canonical JSON that gets signed MUST be
byte-identical to what the relay re-derives. We pin that with
``json.dumps(separators=(",", ":"), sort_keys=True, ensure_ascii=False)`` —
``ensure_ascii=False`` keeps Arabic titles/bodies as raw UTF-8, exactly like
the relay's ``JSON.stringify``, so signatures verify across the two languages.

The event-loop hot path (``_on_state_changed`` runs for EVERY state change in
HA) is a single string-prefix check plus a settled-transition test; only a real
lock edge ever reaches the executor/network.
"""

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

# Notification types carried in the plaintext payload ``data.type``; the app
# branches on these to localize and route the notification.
PUSH_TYPE_SECURITY = "security"
PUSH_TYPE_LOCK = "lock"

# Owner-only audiences: alarm (security), lock, and tank alerts reach the enrolled
# admin only — a room-scoped phone doesn't get them. A LIFE-SAFETY alarm
# (fire/smoke; data.life_safety == "1") is the exception and stays house-wide.
_OWNER_ONLY_TYPES = frozenset(
    {PUSH_TYPE_SECURITY, PUSH_TYPE_LOCK, PUSH_TYPE_TANK_LOW, PUSH_TYPE_TANK_OFFLINE}
)

# Relay priority buckets (must match the relay's accepted values).
PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"

# Lock states. STATE_LOCKED/STATE_UNLOCKED were removed from
# homeassistant.const (they live on lock.LockState now, HA 2025.x+); the wire
# values are stable, so pin the literals here to stay compatible across HA
# versions without depending on the lock component being importable.
STATE_LOCKED = "locked"
STATE_UNLOCKED = "unlocked"

# Lock state machine: the only two states that count as a settled lock, and the
# "old" states that mark a flap rather than a real user action.
_LOCK_PREFIX = "lock."
_LOCK_SETTLED = frozenset({STATE_LOCKED, STATE_UNLOCKED})
_LOCK_FLAP_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

# Widget-refresh coalescing (Phase 8). Control domains whose state a home-screen
# widget renders — a settled edge here is worth a silent refresh. Sensors and
# binary_sensors are deliberately EXCLUDED: their state churns continuously and
# iOS grants only a tight background-push budget, so pushing on every drift would
# exhaust it. Power/temperature tiles ride the widget's own ~1min timeline
# refresh + the foreground app sync instead.
_WIDGET_DOMAINS = frozenset(
    {"light", "switch", "input_boolean", "lock", "cover", "climate", "fan"}
)
# Leading-edge debounce: the FIRST relevant change arms a single flush this many
# seconds out; changes within the window are absorbed into it. Caps widget
# pushes at one per window (a scene flipping many entities → one push) and keeps
# well inside the iOS background-push budget.
_WIDGET_PUSH_COALESCE_SECONDS = 15.0
# States that don't count as a real, settled value for a widget edge.
_WIDGET_UNSETTLED = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

# Per-batch replay nonce: 32 random bytes -> 64 hex chars, comfortably above the
# relay's 32-char floor. (``token_hex(n)`` returns 2n hex chars.)
_NONCE_BYTES = 32


class PushDispatcher:
    """Bridges hub alarm/lock events to signed relay pushes."""

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
        # Pending coalesced widget-refresh flush (None = nothing scheduled).
        self._widget_flush_cancel: Optional[Callable[[], None]] = None

    # -- lifecycle -------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to the alarm + lock bus signals."""
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
        """Release both listeners + any pending flush (idempotent)."""
        if self._unsub_alarm is not None:
            self._unsub_alarm()
            self._unsub_alarm = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._widget_flush_cancel is not None:
            self._widget_flush_cancel()
            self._widget_flush_cancel = None

    # -- bus handlers ----------------------------------------------------------

    @callback
    def _on_alarm_triggered(self, event: Event) -> None:
        """An armed zone / life-safety sensor tripped -> critical push."""
        data = self._build_security_payload(event.data or {})
        self._hass.async_create_task(self._dispatch(data, PRIORITY_CRITICAL))

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Runs for every HA state change — cheap domain guard up front."""
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        # Lock: an immediate, owner-only alert on a settled transition.
        if entity_id.startswith(_LOCK_PREFIX):
            if self._is_real_lock_transition(old_state, new_state):
                data = self._build_lock_payload(entity_id, new_state)
                self._hass.async_create_task(self._dispatch(data, PRIORITY_NORMAL))
            # A lock edge is also widget-relevant (lock tiles) — fall through.

        # Widget refresh: coalesce a settled control-state edge into one silent
        # push (Phase 8). Cheap domain check first so the hot path stays fast.
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
        """Send the coalesced silent widget-refresh push."""
        self._widget_flush_cancel = None
        data = {"type": PUSH_TYPE_UPDATE_WIDGETS, "silent": "1"}
        self._hass.async_create_task(self._dispatch(data, PRIORITY_NORMAL))

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

    # -- payload builders ------------------------------------------------------

    def _build_security_payload(self, alarm_event: dict[str, Any]) -> dict[str, str]:
        """Plaintext security notification from an alarm event dict."""
        life_safety = bool(alarm_event.get("life_safety"))
        entity_id = alarm_event.get("entity_id")
        zone = alarm_event.get("zone")
        name = self._friendly_name(entity_id) or (zone if isinstance(zone, str) else None)
        title = "Life-safety alarm" if life_safety else "Security alarm"
        body = f"{name} triggered the alarm" if name else "The alarm was triggered"
        data = {"type": PUSH_TYPE_SECURITY, "title": title, "body": body}
        if life_safety:
            # House-wide audience flag: a fire/smoke (life-safety) alarm must
            # reach EVERYONE, not just the owner (consumed in _dispatch_inner).
            data["life_safety"] = "1"
        if isinstance(entity_id, str) and entity_id:
            data["entity_id"] = entity_id
        return data

    def _build_lock_payload(
        self, entity_id: str, new_state: Any
    ) -> dict[str, str]:
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

    def _friendly_name(self, entity_id: Any) -> Optional[str]:
        """The entity's friendly name from the live state, or None."""
        if not isinstance(entity_id, str) or not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        name = state.attributes.get("friendly_name")
        return name if isinstance(name, str) and name else None

    # -- dispatch --------------------------------------------------------------

    async def async_send(self, data: dict[str, str], priority: str) -> None:
        """Sign + relay one notification to every registered token.

        The public entry a sibling monitor (``TankPushMonitor``) uses so its
        tank pushes ride the exact same signed-batch relay path as the
        dispatcher's own alarm/lock events — one signing implementation, one
        wire contract. Never raises (``_dispatch`` swallows + logs).
        """
        await self._dispatch(data, priority)

    async def _dispatch(self, data: dict[str, str], priority: str) -> None:
        """Sign + send one notification to every registered device token."""
        try:
            await self._dispatch_inner(data, priority)
        except Exception:  # noqa: BLE001 — a push must never crash the event loop
            _LOGGER.exception("Push dispatch failed for a %s event", data.get("type"))

    async def _dispatch_inner(self, data: dict[str, str], priority: str) -> None:
        try:
            tokens = await self._hass.async_add_executor_job(
                self._push_store.get_all_tokens
            )
        except Exception:  # noqa: BLE001 — storage hiccup must not crash the loop
            _LOGGER.exception("Push dispatch: reading device tokens failed")
            return

        # Owner-only events (alarm / lock / tank) go to the enrolled admin only;
        # a life-safety alarm (data.life_safety) stays house-wide. If roles can't
        # be resolved we fail OPEN (deliver to all) so a safety alert is never
        # silently dropped.
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
        await self._send(body)

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

    async def _send(self, body: dict[str, Any]) -> None:
        """POST the signed batch to the relay; clean up dead tokens on reply."""
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
        """Best-effort JSON parse of a relay response, or None."""
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError):
            return None

    async def _cleanup_dead_tokens(self, payload: Any) -> None:
        """Drop any token the relay flagged ``remove_token`` (UNREGISTERED)."""
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
        except Exception:  # noqa: BLE001 — cleanup is best-effort, never fatal
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


# -- Tank monitor (B8 Piece 4b) ------------------------------------------------
# The low-water sweep runs at 15:00 UTC == 18:00 AST (KSA is UTC+3, no DST), so
# the schedule is timezone-independent of however HA itself is configured.
TANK_LOW_CHECK_UTC_HOUR = 15
# A tank silent this long (the script POSTs every 5 min) counts as offline.
TANK_OFFLINE_TIMEOUT_SECONDS = 20 * 60
# The offline watchdog cadence — well under the 20-minute threshold so an
# outage is caught within one poll of crossing it.
TANK_OFFLINE_POLL = timedelta(minutes=5)
# AST day bucketing for the once-per-device-per-day dedup.
_SECONDS_PER_DAY = 86400
_AST_OFFSET_SECONDS = 3 * 3600


class TankPushMonitor:
    """Water-tank low-level + offline push notifications (B8 Piece 4b).

    Unlike the alarm/lock dispatcher this is **timer-driven, not event-driven**:
    tank readings arrive on a 5-minute REST cadence, and "low at 6pm" / "silent
    for 20 minutes" are time questions, not state edges. Two timers:

    - a daily 18:00-AST sweep: for every calibrated tank, compute the current
      percent and push once if it's below the tank's ``low_percent``;
    - a 5-minute watchdog: push once when a tank that *was* reporting has gone
      silent for 20+ minutes.

    Both are deduped to at most one push per device per **AST calendar day** (so
    the offline watchdog can't fire every 5 minutes). Each alert also fires its
    HA bus event (EVENT_TANK_LOW / EVENT_TANK_OFFLINE) as the installer
    automation hook, then dispatches the phone push through the shared signed
    relay path (``PushDispatcher.async_send``).

    The decision methods (``async_check_low_water`` / ``async_check_offline``)
    take their "now" from an injectable ``clock`` and read the engine via the
    executor, so they're unit-testable without HA timers or a live relay.
    """

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
        # device_id -> AST day index of the last push, one map per channel so a
        # low-water push and an offline push don't suppress each other.
        self._low_pushed_day: dict[str, int] = {}
        self._offline_pushed_day: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Wire the two timers. Imported lazily so the module stays importable
        (the dispatcher unit tests stub only ``homeassistant.const``/``core``).
        """
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
        """Release both timers (idempotent — safe on any unload)."""
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
        day = self._ast_day(now)
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
            except Exception:  # noqa: BLE001 — one bad tank must not stop the sweep
                _LOGGER.exception("Tank %s: status read failed", device_id)
                continue
            if status.get("percent") is None or not status.get("is_low"):
                continue
            self._low_pushed_day[device_id] = day
            await self._emit_low(device, status)

    async def async_check_offline(self) -> None:
        """Push once for each previously-reporting tank now silent 20+ min."""
        now = self._clock()
        day = self._ast_day(now)
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

    # -- internals -------------------------------------------------------------

    async def _list_devices(self) -> list[dict[str, Any]]:
        try:
            return await self._hass.async_add_executor_job(self._tanks.list_devices)
        except Exception:  # noqa: BLE001 — a storage hiccup skips this tick only
            _LOGGER.exception("Tank monitor: listing devices failed")
            return []

    @staticmethod
    def _ast_day(now: float) -> int:
        """The AST (UTC+3) calendar-day index for the once-per-day dedup."""
        return int((now + _AST_OFFSET_SECONDS) // _SECONDS_PER_DAY)
