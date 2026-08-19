"""CasaSmart runtime component."""

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



_SIREN_KINDS = frozenset({EVENT_TRIGGERED, EVENT_LIFE_SAFETY})


class AlarmAdapter:
    """CasaSmart runtime component."""

    def __init__(self, hass: HomeAssistant, engine: AlarmEngine) -> None:
        self._hass = hass
        self._engine = engine
        self._unsub_state_changed: Optional[Callable[[], None]] = None
        self._unsub_alarm_changed: Optional[Callable[[], None]] = None

        self._cancel_timer: Optional[Callable[[], None]] = None



    @callback
    def async_start(self) -> None:
        """CasaSmart runtime component."""
        self._unsub_state_changed = self._hass.bus.async_listen(
            "state_changed", self._on_state_changed
        )
        self._unsub_alarm_changed = self._hass.bus.async_listen(
            EVENT_ALARM_CHANGED, self._on_alarm_changed
        )


        self._sync_pending_timer()

    @callback
    def async_stop(self) -> None:
        """CasaSmart runtime component."""
        if self._unsub_state_changed is not None:
            self._unsub_state_changed()
            self._unsub_state_changed = None
        if self._unsub_alarm_changed is not None:
            self._unsub_alarm_changed()
            self._unsub_alarm_changed = None
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None



    @callback
    def _on_state_changed(self, event: Event) -> None:
        """CasaSmart runtime component."""
        entity_id = event.data.get("entity_id")
        if entity_id is None or self._engine.zone_of(entity_id) is None:
            return
        new_state = event.data.get("new_state")
        self._hass.async_create_task(self._evaluate(entity_id, new_state))

    async def _evaluate(self, entity_id: str, new_state: Any) -> None:
        """CasaSmart runtime component."""
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



    @callback
    def _on_alarm_changed(self, _event: Event) -> None:
        """CasaSmart runtime component."""
        self._sync_pending_timer()

    @callback
    def _sync_pending_timer(self) -> None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        self._cancel_timer = None
        self._hass.async_create_task(self._run_tick())

    async def _run_tick(self) -> None:
        try:
            alarm_event = await self._hass.async_add_executor_job(self._engine.tick)
        except Exception:
            _LOGGER.exception("Alarm tick failed")
            return
        self._react(alarm_event)



    @callback
    def _react(self, alarm_event: Optional[dict[str, Any]]) -> None:
        """CasaSmart runtime component."""
        if alarm_event is None:
            return
        self._notify_changed()
        if alarm_event.get("kind") in _SIREN_KINDS:
            self._hass.bus.async_fire(EVENT_ALARM_TRIGGERED, dict(alarm_event))

    @callback
    def _notify_changed(self) -> None:
        """CasaSmart runtime component."""
        self._hass.bus.async_fire(
            EVENT_ALARM_CHANGED, {"mode": self._engine.snapshot()["mode"]}
        )
