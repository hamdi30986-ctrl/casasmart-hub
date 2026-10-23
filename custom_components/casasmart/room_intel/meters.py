"""Power meters for the room ranking: is a sensor one, and what does it read.

A meter is a sensor with device class power. ranking decides which meters
measure a room (its own devices' meters, never a standalone or whole-home
one) and sums their readings. A reading that can't be trusted is None, never
zero: unavailable, unknown, not a number, no power unit, or below
-NEGATIVE_TOLERANCE_W (a meter wired or signed the wrong way round; abs()
would turn it into the biggest load in the house).

Age is not judged: Home Assistant's MQTT entities write a state only when its
value changes, so a steady reading's last_reported doesn't move, and a
meter that stops reporting goes unavailable instead.

power.power_sensor_of is the Energy Saving wizard's lookup and keeps its own
rule. This module has no Home Assistant import, like activity.
"""

from __future__ import annotations

import math
from typing import Any

# Home Assistant's power units (homeassistant.const.UnitOfPower), in watts
# (homeassistant.util.unit_conversion.PowerConverter).
_WATTS_PER_UNIT = {
    "mW": 0.001,
    "W": 1.0,
    "kW": 1000.0,
    "MW": 1e6,
    "GW": 1e9,
    "TW": 1e12,
    "BTU/h": 0.29307107,
}
# A meter at rest may read a little below zero; that reads as 0 W.
NEGATIVE_TOLERANCE_W = 2.0
# Watts are compared in steps this far apart (10 %), so meter noise doesn't
# swap two rooms on every refresh.
_STEP_RATIO = 1.1


def is_power_meter(state: Any) -> bool:
    """True for a sensor whose device class is power."""
    return (
        state.entity_id.startswith("sensor.")
        and state.attributes.get("device_class") == "power"
    )


def watts_of(state: Any) -> float | None:
    """The meter's reading in watts, or None when it can't be trusted."""
    factor = _WATTS_PER_UNIT.get(state.attributes.get("unit_of_measurement"))
    if factor is None:
        return None
    try:
        watts = float(state.state) * factor
    except (TypeError, ValueError):
        return None  # unavailable, unknown, or not a number
    if not math.isfinite(watts) or watts < -NEGATIVE_TOLERANCE_W:
        return None
    return max(watts, 0.0)


def watt_step(watts: float) -> int:
    """The 10 % step a reading falls in; -1 below 1 W.

    Readings more than 10 % apart are always in different steps, the bigger
    one in the higher step.
    """
    if watts < 1:
        return -1
    return math.floor(math.log(watts) / math.log(_STEP_RATIO))
