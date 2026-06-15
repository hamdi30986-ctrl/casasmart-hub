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

Water/tank events are deliberately NOT here — that is Piece 4b.

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
from typing import TYPE_CHECKING, Any, Callable, Optional

import aiohttp

from homeassistant.const import (
    STATE_LOCKED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    STATE_UNLOCKED,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import EVENT_ALARM_TRIGGERED, PUSH_RELAY_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .push import PushTokenStore
    from .push_crypto import PushSigner

_LOGGER = logging.getLogger(__name__)

# Notification types carried in the plaintext payload ``data.type``; the app
# branches on these to localize and route the notification.
PUSH_TYPE_SECURITY = "security"
PUSH_TYPE_LOCK = "lock"

# Relay priority buckets (must match the relay's accepted values).
PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"

# Lock state machine: the only two states that count as a settled lock, and the
# "old" states that mark a flap rather than a real user action.
_LOCK_PREFIX = "lock."
_LOCK_SETTLED = frozenset({STATE_LOCKED, STATE_UNLOCKED})
_LOCK_FLAP_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

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
        """Release both listeners (idempotent — safe on any unload)."""
        if self._unsub_alarm is not None:
            self._unsub_alarm()
            self._unsub_alarm = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    # -- bus handlers ----------------------------------------------------------

    @callback
    def _on_alarm_triggered(self, event: Event) -> None:
        """An armed zone / life-safety sensor tripped -> critical push."""
        data = self._build_security_payload(event.data or {})
        self._hass.async_create_task(self._dispatch(data, PRIORITY_CRITICAL))

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Runs for every HA state change — reject non-lock edges cheaply."""
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith(_LOCK_PREFIX):
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not self._is_real_lock_transition(old_state, new_state):
            return
        data = self._build_lock_payload(entity_id, new_state)
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

        device_tokens = [
            rec["fcm_token"]
            for rec in tokens.values()
            if isinstance(rec.get("fcm_token"), str) and rec["fcm_token"]
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
