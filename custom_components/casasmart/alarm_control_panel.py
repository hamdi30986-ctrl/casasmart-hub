"""CasaSmart runtime component."""

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



_HA_ACTOR = "homeassistant"




_MODE_TO_STATE: dict[str, AlarmControlPanelState] = {
    MODE_DISARMED: AlarmControlPanelState.DISARMED,
    MODE_AWAY: AlarmControlPanelState.ARMED_AWAY,
    MODE_HOME: AlarmControlPanelState.ARMED_HOME,
    MODE_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
    MODE_PENDING: AlarmControlPanelState.PENDING,
    MODE_TRIGGERED: AlarmControlPanelState.TRIGGERED,
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """CasaSmart runtime component."""
    engine: AlarmEngine = entry.runtime_data.alarm
    async_add_entities([CasaSmartAlarmPanel(hass, entry.entry_id, engine)])


class CasaSmartAlarmPanel(AlarmControlPanelEntity):
    """CasaSmart runtime component."""

    _attr_has_entity_name = True
    _attr_name = "Security"

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



    async def async_added_to_hass(self) -> None:
        """CasaSmart runtime component."""
        self._unsub_changed = self._hass.bus.async_listen(
            EVENT_ALARM_CHANGED, self._on_alarm_changed
        )
        self._schedule_arming_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_changed is not None:
            self._unsub_changed()
            self._unsub_changed = None
        self._cancel_arming_refresh()



    @property
    def alarm_state(self) -> AlarmControlPanelState:
        """CasaSmart runtime component."""
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



    async def async_alarm_disarm(self, code: Optional[str] = None) -> None:
        await self._command(self._engine.disarm)

    async def async_alarm_arm_away(self, code: Optional[str] = None) -> None:
        await self._command(partial(self._engine.arm, MODE_AWAY))

    async def async_alarm_arm_home(self, code: Optional[str] = None) -> None:
        await self._command(partial(self._engine.arm, MODE_HOME))

    async def async_alarm_arm_night(self, code: Optional[str] = None) -> None:
        await self._command(partial(self._engine.arm, MODE_NIGHT))

    async def _command(self, engine_call) -> None:
        """CasaSmart runtime component."""
        await self._hass.async_add_executor_job(
            partial(engine_call, actor=_HA_ACTOR)
        )
        self._hass.bus.async_fire(EVENT_ALARM_CHANGED, {})



    @callback
    def _on_alarm_changed(self, _event: Event) -> None:
        """CasaSmart runtime component."""
        self._schedule_arming_refresh()
        self.async_write_ha_state()

    @callback
    def _schedule_arming_refresh(self) -> None:
        """CasaSmart runtime component."""
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
