"""Hub-side alarm adapter (Phase 6, block B13) — the Home Assistant glue.

The pure decision engine lives in ``alarm.py`` (stdlib only, unit-tested on a
temp DB). This module is everything that engine deliberately does NOT do: it
wires the engine to live Home Assistant. Specifically it

- subscribes to ``state_changed`` and feeds **mapped** sensor edges into
  ``AlarmEngine.process_sensor`` (offloaded to the executor — the engine
  writes to SQLite on a triggering edge),
- treats a mapped sensor going ``unavailable``/``unknown`` (or vanishing)
  while armed as tamper (``process_sensor_offline``),
- runs ONE exact timer per entry-delay countdown (``pending`` ->
  ``tick``), rescheduled from the engine's own deadline rather than polling,
- fires ``EVENT_ALARM_CHANGED`` on every transition so the WS server can
  nudge connected apps and so this adapter re-syncs its own timer (which is
  how an app-driven disarm cancels a running countdown), and
- fires ``EVENT_ALARM_TRIGGERED`` — carrying the alarm event — ONLY when an
  armed zone or a life-safety sensor actually trips. That bus event is the
  installer's automation hook (siren, lights flash, "whatever Hamdi
  configures per client", plan B13). Tamper does not fire it.

What this module pointedly does NOT touch: the engine's ``alert_sink``. That
is the encrypted-push seam (B8), left at its no-op default until the relay
ships — the siren leg (HA automation) is a separate concern from the
push-to-a-dead-app's-phone leg, so wiring one doesn't pre-empt the other.

The event-loop hot path (``_on_state_changed`` runs for EVERY state change in
HA) is a single in-memory ``zone_of`` dict lookup; only edges on mapped
sensors ever reach the executor.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .alarm import AlarmEngine, EVENT_LIFE_SAFETY, EVENT_TRIGGERED
from .const import EVENT_ALARM_CHANGED, EVENT_ALARM_TRIGGERED

_LOGGER = logging.getLogger(__name__)

# Alarm event kinds that should sound the house (fire EVENT_ALARM_TRIGGERED).
# Tamper is deliberately excluded — a dead battery must not wake everyone.
_SIREN_KINDS = frozenset({EVENT_TRIGGERED, EVENT_LIFE_SAFETY})


class AlarmAdapter:
    """Bridges the pure ``AlarmEngine`` to live Home Assistant events."""

    def __init__(self, hass: HomeAssistant, engine: AlarmEngine) -> None:
        self._hass = hass
        self._engine = engine
        self._unsub_state_changed: Optional[Callable[[], None]] = None
        self._unsub_alarm_changed: Optional[Callable[[], None]] = None
        # The single live entry-delay timer (None when no countdown runs).
        self._cancel_timer: Optional[Callable[[], None]] = None

    # -- lifecycle -------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to HA events and arm the timer for any restored state."""
        self._unsub_state_changed = self._hass.bus.async_listen(
            "state_changed", self._on_state_changed
        )
        self._unsub_alarm_changed = self._hass.bus.async_listen(
            EVENT_ALARM_CHANGED, self._on_alarm_changed
        )
        # warm_up already fails a restored mid-countdown to triggered, so this
        # is normally a no-op — but it keeps the timer authoritative from t=0.
        self._sync_pending_timer()

    @callback
    def async_stop(self) -> None:
        """Release listeners + the timer (idempotent — safe on any unload)."""
        if self._unsub_state_changed is not None:
            self._unsub_state_changed()
            self._unsub_state_changed = None
        if self._unsub_alarm_changed is not None:
            self._unsub_alarm_changed()
            self._unsub_alarm_changed = None
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    # -- sensor edges (hot path) -----------------------------------------------

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Runs for every HA state change — reject unmapped edges cheaply.

        The guard is a pure in-memory dict lookup; only edges on sensors the
        alarm actually watches pay for the executor hop that follows.
        """
        entity_id = event.data.get("entity_id")
        if entity_id is None or self._engine.zone_of(entity_id) is None:
            return
        new_state = event.data.get("new_state")
        self._hass.async_create_task(self._evaluate(entity_id, new_state))

    async def _evaluate(self, entity_id: str, new_state: Any) -> None:
        """Feed one mapped edge to the engine off-loop, then react."""
        offline = new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )
        try:
            if offline:
                # Mapped sensor dropped offline — tamper while armed, ignored
                # while disarmed. Never triggers the full alarm.
                alarm_event = await self._hass.async_add_executor_job(
                    self._engine.process_sensor_offline, entity_id
                )
            else:
                active = new_state.state == STATE_ON
                alarm_event = await self._hass.async_add_executor_job(
                    self._engine.process_sensor, entity_id, active
                )
        except Exception:  # noqa: BLE001 — a sensor edge must never crash setup
            _LOGGER.exception("Alarm evaluation failed for %s", entity_id)
            return
        self._react(alarm_event)

    # -- entry-delay timer -----------------------------------------------------

    @callback
    def _on_alarm_changed(self, _event: Event) -> None:
        """Any transition (incl. an app-driven arm/disarm) re-syncs the timer.

        This is how disarming through the API cancels a running countdown: the
        API fires ``EVENT_ALARM_CHANGED``, ``pending_deadline`` is now ``None``,
        and the live timer is dropped here.
        """
        self._sync_pending_timer()

    @callback
    def _sync_pending_timer(self) -> None:
        """Make exactly one timer match the engine's current entry-delay."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        deadline = self._engine.pending_deadline()
        if deadline is None:
            return
        delay = max(0.0, deadline - time.time())
        self._cancel_timer = async_call_later(
            self._hass, delay, self._on_pending_expired
        )

    @callback
    def _on_pending_expired(self, _now: Any) -> None:
        """The countdown lapsed — promote pending -> triggered off-loop."""
        self._cancel_timer = None
        self._hass.async_create_task(self._run_tick())

    async def _run_tick(self) -> None:
        try:
            alarm_event = await self._hass.async_add_executor_job(self._engine.tick)
        except Exception:  # noqa: BLE001 — a storage hiccup must not kill the loop
            _LOGGER.exception("Alarm tick failed")
            return
        self._react(alarm_event)

    # -- reactions -------------------------------------------------------------

    @callback
    def _react(self, alarm_event: Optional[dict[str, Any]]) -> None:
        """Fan a state transition out to the bus (state push + siren hook)."""
        if alarm_event is None:
            return  # the common no-op edge: nothing changed
        self._notify_changed()
        if alarm_event.get("kind") in _SIREN_KINDS:
            self._hass.bus.async_fire(EVENT_ALARM_TRIGGERED, dict(alarm_event))

    @callback
    def _notify_changed(self) -> None:
        """Tell the WS server + this adapter's timer that the state moved."""
        self._hass.bus.async_fire(
            EVENT_ALARM_CHANGED, {"mode": self._engine.snapshot()["mode"]}
        )
