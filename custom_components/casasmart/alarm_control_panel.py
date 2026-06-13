"""CasaSmart alarm_control_panel entity (Phase 6, block B13) — the HA face.

The hub-authoritative ``AlarmEngine`` (``alarm.py``) owns every decision; the
REST API (``alarm_api.py``) is how the *app* drives it. This platform is the
third face: a first-class Home Assistant ``alarm_control_panel`` entity that
mirrors the engine's arm state, so

- arm/disarm shows up in HA dashboards and the logbook, and
- installer automations trigger on a standard ``alarm_control_panel`` state
  (``triggered`` / ``armed_away`` / …) instead of only the custom
  ``EVENT_ALARM_TRIGGERED`` bus event. The breach->siren hook keeps working as
  before; this just adds the native, arm-state-aware trigger surface.

Both directions ride one engine. Arming through HA routes into the same
``AlarmEngine.arm`` the app calls and fires ``EVENT_ALARM_CHANGED``; arming
through the app fires that same event, which this entity listens to — so a
phone-driven arm reflects on the HA panel and vice-versa, with no second
source of truth.

Option A (no PIN): ``code_arm_required`` is False and there is no code format.
Authorisation lives at the app/JWT layer and at HA's own auth — the panel does
not gate on a shared secret (see B13 decision log).

Exit-delay grace is passive in the engine (no event fires when it lapses), so
when the panel computes ``ARMING`` it schedules one exact refresh at the
engine's ``arming_until`` to flip the displayed state to fully-armed — the same
one-timer posture the adapter uses for the entry-delay countdown.
"""

from __future__ import annotations

import logging
import time
from functools import partial
from typing import Any, Optional

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later

from .alarm import (
    ARMABLE_MODES,
    MODE_AWAY,
    MODE_DISARMED,
    MODE_HOME,
    MODE_NIGHT,
    MODE_PENDING,
    MODE_TRIGGERED,
    AlarmEngine,
)
from .const import DOMAIN, EVENT_ALARM_CHANGED

_LOGGER = logging.getLogger(__name__)

# Arming through the HA panel/automations is recorded under this actor in the
# engine's audit trail — it did not come from a CasaSmart app user.
_HA_ACTOR = "homeassistant"

# Steady (grace consumed) engine mode -> HA panel state. ARMING is derived
# separately from ``arming_until`` because the engine has no distinct exit
# state — a freshly armed mode is "arming" only until its grace deadline.
_MODE_TO_STATE: dict[str, AlarmControlPanelState] = {
    MODE_DISARMED: AlarmControlPanelState.DISARMED,
    MODE_AWAY: AlarmControlPanelState.ARMED_AWAY,
    MODE_HOME: AlarmControlPanelState.ARMED_HOME,
    MODE_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
    MODE_PENDING: AlarmControlPanelState.PENDING,
    MODE_TRIGGERED: AlarmControlPanelState.TRIGGERED,
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Register the single CasaSmart alarm panel for this hub entry."""
    engine: AlarmEngine = entry.runtime_data.alarm
    async_add_entities([CasaSmartAlarmPanel(hass, entry.entry_id, engine)])


class CasaSmartAlarmPanel(AlarmControlPanelEntity):
    """One panel mirroring the hub-authoritative ``AlarmEngine``."""

    _attr_has_entity_name = True
    _attr_name = "Security"
    # Option A: no PIN. Authorisation is the app's JWT / HA's own auth.
    _attr_code_arm_required = False
    _attr_code_format = None
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(
        self, hass: HomeAssistant, entry_id: str, engine: AlarmEngine
    ) -> None:
        self._hass = hass
        self._engine = engine
        self._attr_unique_id = f"{entry_id}_alarm"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )
        self._unsub_changed: Optional[Any] = None
        self._cancel_arming: Optional[Any] = None

    # -- lifecycle -------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Track every arm-state transition (app- or HA-driven) and follow it."""
        self._unsub_changed = self._hass.bus.async_listen(
            EVENT_ALARM_CHANGED, self._on_alarm_changed
        )
        self._schedule_arming_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_changed is not None:
            self._unsub_changed()
            self._unsub_changed = None
        self._cancel_arming_refresh()

    # -- state -----------------------------------------------------------------

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        """Live engine mode -> HA panel state (ARMING derived from grace)."""
        snap = self._engine.snapshot()
        mode = snap["mode"]
        arming_until = snap.get("arming_until")
        if (
            mode in ARMABLE_MODES
            and arming_until is not None
            and time.time() < arming_until
        ):
            return AlarmControlPanelState.ARMING
        return _MODE_TO_STATE.get(mode, AlarmControlPanelState.DISARMED)

    # -- commands (route into the one engine, then announce) -------------------

    async def async_alarm_disarm(self, code: Optional[str] = None) -> None:
        await self._command(self._engine.disarm)

    async def async_alarm_arm_away(self, code: Optional[str] = None) -> None:
        await self._command(partial(self._engine.arm, MODE_AWAY))

    async def async_alarm_arm_home(self, code: Optional[str] = None) -> None:
        await self._command(partial(self._engine.arm, MODE_HOME))

    async def async_alarm_arm_night(self, code: Optional[str] = None) -> None:
        await self._command(partial(self._engine.arm, MODE_NIGHT))

    async def _command(self, engine_call) -> None:
        """Run a storage-touching engine call off-loop, then fan the change out.

        Firing ``EVENT_ALARM_CHANGED`` is what keeps the three faces in sync:
        the WS server nudges connected apps, the adapter re-syncs its
        entry-delay timer, and our own listener refreshes this entity's state.
        """
        await self._hass.async_add_executor_job(
            partial(engine_call, actor=_HA_ACTOR)
        )
        self._hass.bus.async_fire(EVENT_ALARM_CHANGED, {})

    # -- transitions -----------------------------------------------------------

    @callback
    def _on_alarm_changed(self, _event: Event) -> None:
        """Any arm-state move: repaint now and re-arm the grace refresh."""
        self._schedule_arming_refresh()
        self.async_write_ha_state()

    @callback
    def _schedule_arming_refresh(self) -> None:
        """If we're inside an exit-grace window, schedule the flip to armed.

        The engine fires no event when grace lapses (it's a passive deadline),
        so without this the panel would sit on ``ARMING`` until the next
        unrelated transition. Exactly one timer, rescheduled on every change.
        """
        self._cancel_arming_refresh()
        snap = self._engine.snapshot()
        arming_until = snap.get("arming_until")
        if snap["mode"] not in ARMABLE_MODES or arming_until is None:
            return
        delay = arming_until - time.time()
        if delay <= 0:
            return
        self._cancel_arming = async_call_later(
            self._hass, delay, self._on_arming_done
        )

    @callback
    def _on_arming_done(self, _now: Any) -> None:
        self._cancel_arming = None
        self.async_write_ha_state()

    @callback
    def _cancel_arming_refresh(self) -> None:
        if self._cancel_arming is not None:
            self._cancel_arming()
            self._cancel_arming = None
