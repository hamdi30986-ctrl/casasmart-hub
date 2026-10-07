"""Connects AlarmEngine to Home Assistant.

Feeds state changes of the alarm's sensors to the engine (a sensor going
unavailable, unknown or missing is tamper while armed), runs the entry-delay
timer and fires EVENT_ALARM_CHANGED on every transition.
EVENT_ALARM_TRIGGERED fires only when the alarm goes off (an armed zone or
life safety, not tamper): it is the installer's siren hook, and the push
dispatcher sends the phone alert from it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .alarm import EVENT_LIFE_SAFETY, EVENT_TRIGGERED, AlarmEngine
from .const import EVENT_ALARM_CHANGED, EVENT_ALARM_TRIGGERED

_LOGGER = logging.getLogger(__name__)

# Event kinds that fire EVENT_ALARM_TRIGGERED. Tamper is left out: a dead
# battery shouldn't wake everyone.
_SIREN_KINDS = frozenset({EVENT_TRIGGERED, EVENT_LIFE_SAFETY})


class AlarmAdapter:
    """Drives AlarmEngine from Home Assistant events."""

    def __init__(self, hass: HomeAssistant, engine: AlarmEngine) -> None:
        self._hass = hass
        self._engine = engine
        self._unsub_state_changed: Callable[[], None] | None = None
        self._unsub_alarm_changed: Callable[[], None] | None = None
        # Cancels the entry-delay timer; None when no countdown runs.
        self._cancel_timer: Callable[[], None] | None = None

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
        # Normally a no-op: warm_up turns a restored countdown into a trigger.
        self._sync_pending_timer()

    @callback
    def async_stop(self) -> None:
        """Remove the listeners and the timer (idempotent)."""
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
        """Called for every HA state change; skips sensors the alarm ignores.

        The check is one in-memory lookup, so only the alarm's own sensors
        cost an executor job.
        """
        entity_id = event.data.get("entity_id")
        if entity_id is None or self._engine.zone_of(entity_id) is None:
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        # Attribute-only updates aren't edges: an open window mustn't trip the
        # alarm on its next battery report. A change from unavailable counts.
        if (
            old_state is not None
            and new_state is not None
            and old_state.state == new_state.state
        ):
            return
        self._hass.async_create_task(self._evaluate(entity_id, new_state))

    async def _evaluate(self, entity_id: str, new_state: Any) -> None:
        """Pass one edge to the engine in the executor, then react."""
        offline = new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )
        try:
            if offline:
                alarm_event = await self._hass.async_add_executor_job(
                    self._engine.process_sensor_offline, entity_id
                )
            else:
                active = new_state.state == STATE_ON
                alarm_event = await self._hass.async_add_executor_job(
                    self._engine.process_sensor, entity_id, active
                )
        except Exception:
            _LOGGER.exception("Alarm evaluation failed for %s", entity_id)
            return
        self._react(alarm_event)

    # -- entry-delay timer -----------------------------------------------------

    @callback
    def _on_alarm_changed(self, _event: Event) -> None:
        """Re-sync the entry-delay timer after any transition.

        This is how an app disarm cancels a running countdown:
        pending_deadline() is now None, so the timer is dropped.
        """
        self._sync_pending_timer()

    @callback
    def _sync_pending_timer(self) -> None:
        """Replace the timer with one for the engine's current entry delay."""
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
        """The entry delay ran out: run the engine's tick in the executor."""
        self._cancel_timer = None
        self._hass.async_create_task(self._run_tick())

    async def _run_tick(self) -> None:
        """Run the engine's tick, then react (or re-arm if it fired early)."""
        try:
            alarm_event = await self._hass.async_add_executor_job(self._engine.tick)
        except Exception:
            _LOGGER.exception("Alarm tick failed")
            return
        if alarm_event is None:
            # The timer uses the loop's monotonic clock and the engine wall-clock
            # time, so it can fire a moment early. Re-arm for the remainder.
            self._sync_pending_timer()
            return
        self._react(alarm_event)

    # -- reactions -------------------------------------------------------------

    @callback
    def _react(self, alarm_event: dict[str, Any] | None) -> None:
        """Announce a transition, plus the siren hook when the alarm goes off."""
        if alarm_event is None:
            return  # nothing changed
        self._notify_changed()
        if alarm_event.get("kind") in _SIREN_KINDS:
            self._hass.bus.async_fire(EVENT_ALARM_TRIGGERED, dict(alarm_event))

    @callback
    def _notify_changed(self) -> None:
        """Fire EVENT_ALARM_CHANGED for the WS server and this adapter's timer."""
        self._hass.bus.async_fire(
            EVENT_ALARM_CHANGED, {"mode": self._engine.snapshot()["mode"]}
        )
