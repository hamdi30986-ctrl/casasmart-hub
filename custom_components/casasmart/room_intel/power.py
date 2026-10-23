"""Power meters: which sensor reports a device's power draw.

Energy Saving's wizard shows it for each plug.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


def power_sensor_of(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """The power sensor on the device's HA device, if there is one.

    That is the first sibling sensor, by entity id, with device class power
    or "power" in its entity id.
    """
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    siblings = er.async_entries_for_device(
        registry, entry.device_id, include_disabled_entities=True
    )
    for sibling in sorted(siblings, key=lambda item: item.entity_id):
        if not sibling.entity_id.startswith("sensor."):
            continue
        state = hass.states.get(sibling.entity_id)
        device_class = (
            state.attributes.get("device_class") if state is not None else None
        ) or sibling.original_device_class
        device_class = getattr(device_class, "value", device_class)
        if (
            str(device_class or "").lower() != "power"
            and "power" not in sibling.entity_id
        ):
            continue
        return {
            "entity_id": sibling.entity_id,
            "state": state.state if state is not None else "unavailable",
            "unit": state.attributes.get("unit_of_measurement")
            if state is not None
            else None,
        }
    return None
