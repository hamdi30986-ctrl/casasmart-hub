"""``alarm_control_panel`` stand-ins (moved verbatim from
tests/test_alarm_control_panel.py's per-suite stub)."""

import enum


class AlarmControlPanelEntityFeature(enum.IntFlag):
    ARM_HOME = 1
    ARM_AWAY = 2
    ARM_NIGHT = 4


class AlarmControlPanelState(enum.StrEnum):
    DISARMED = "disarmed"
    ARMED_AWAY = "armed_away"
    ARMED_HOME = "armed_home"
    ARMED_NIGHT = "armed_night"
    PENDING = "pending"
    ARMING = "arming"
    TRIGGERED = "triggered"


class AlarmControlPanelEntity:
    """Minimal base: ``async_write_ha_state`` is a no-op the panel can call.

    It does NOT count writes any more. A suite that wants to observe repaints
    overrides the method on the instance (see test_alarm_control_panel), which
    works against real Home Assistant too — counting through this base tied
    the assertion to the stubbed environment, where the real Entity's
    ``async_write_ha_state`` would have demanded a fully registered entity.
    """

    def async_write_ha_state(self) -> None:
        """No-op — the panel only needs the call to succeed."""
