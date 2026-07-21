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
    """Minimal base: records async_write_ha_state calls.

    ``write_count`` is a class attribute (not set in __init__) because the
    real-world entity, like ours, does not call ``super().__init__()``.
    """

    write_count = 0

    def async_write_ha_state(self) -> None:
        self.write_count += 1
