"""The alarm as a Home Assistant alarm_control_panel entity.

AlarmEngine makes every decision; this entity mirrors its arm state, so it
shows on HA dashboards and in the logbook, and installers can automate on
standard panel states. Arming from HA calls the same engine as the app, and
both fire EVENT_ALARM_CHANGED, which the entity follows. There is no PIN:
access is controlled by the app's tokens and HA's own auth.
"""

from __future__ import annotations

import time
from functools import partial
from typing import Any

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

# Actor recorded in the alarm history for commands from HA.
_HA_ACTOR = "homeassistant"

# Engine mode -> panel state. ARMING has no engine mode: alarm_state derives
# it from arming_until.
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
    """The hub's alarm panel, mirroring AlarmEngine."""

    _attr_has_entity_name = True
    _attr_name = "Security"
    _attr_code_arm_required = False
    _attr_code_format = None
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(self, hass: HomeAssistant, entry_id: str, engine: AlarmEngine) -> None:
        self._hass = hass
        self._engine = engine
        self._attr_unique_id = f"{entry_id}_alarm"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )
        self._unsub_changed: Any | None = None
        self._cancel_arming: Any | None = None

    # -- lifecycle -------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Follow every arm-state change, from the app or from HA."""
        self._unsub_changed = self._hass.bus.async_listen(
            EVENT_ALARM_CHANGED, self._on_alarm_changed
        )
        self._schedule_arming_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Remove the listener and any pending exit-delay refresh."""
        if self._unsub_changed is not None:
            self._unsub_changed()
            self._unsub_changed = None
        self._cancel_arming_refresh()

    # -- state -----------------------------------------------------------------

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        """The engine mode as a panel state; ARMING while the exit delay runs."""
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

    # -- commands --------------------------------------------------------------

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._command(self._engine.disarm)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._command(partial(self._engine.arm, MODE_AWAY))

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._command(partial(self._engine.arm, MODE_HOME))

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._command(partial(self._engine.arm, MODE_NIGHT))

    async def _command(self, engine_call) -> None:
        """Run an engine call in the executor, then fire EVENT_ALARM_CHANGED.

        The event updates connected apps, the adapter's entry-delay timer and
        this entity's state.
        """
        await self._hass.async_add_executor_job(partial(engine_call, actor=_HA_ACTOR))
        self._hass.bus.async_fire(EVENT_ALARM_CHANGED, {})

    # -- transitions -----------------------------------------------------------

    @callback
    def _on_alarm_changed(self, _event: Event) -> None:
        """Repaint, and reschedule the exit-delay refresh."""
        self._schedule_arming_refresh()
        self.async_write_ha_state()

    @callback
    def _schedule_arming_refresh(self) -> None:
        """During the exit delay, schedule a repaint for when it ends.

        The engine fires no event when the exit delay ends, so without this
        the panel would show ARMING until the next transition.
        """
        self._cancel_arming_refresh()
        snap = self._engine.snapshot()
        arming_until = snap.get("arming_until")
        if snap["mode"] not in ARMABLE_MODES or arming_until is None:
            return
        delay = arming_until - time.time()
        if delay <= 0:
            return
        self._cancel_arming = async_call_later(self._hass, delay, self._on_arming_done)

    @callback
    def _on_arming_done(self, _now: Any) -> None:
        """The exit delay ended: repaint ARMING as the armed state."""
        self._cancel_arming = None
        self.async_write_ha_state()

    @callback
    def _cancel_arming_refresh(self) -> None:
        """Cancel the pending exit-delay refresh, if any."""
        if self._cancel_arming is not None:
            self._cancel_arming()
            self._cancel_arming = None
