"""HA-aware entity filtering: what is served, which room it is in, and scope.

Combines the entity_bridge rules with HA's registries and the CasaSmart
registry to resolve rooms, check room scope and serialize devices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)

from .const import DOMAIN
from .entity_bridge import is_category_served, is_exposed, serialize_state
from .registry import UNSET

if TYPE_CHECKING:
    from .registry import RegistryEngine


def get_registry_engine(hass: HomeAssistant) -> RegistryEngine | None:
    """The loaded entry's registry engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    return entries[0].runtime_data.registry


def ha_area_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """HA's own area resolution (entity override first, then device)."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    area_id = entry.area_id
    if area_id is None and entry.device_id is not None:
        device = dr.async_get(hass).async_get(entry.device_id)
        area_id = device.area_id if device else None
    return area_id


def area_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """An entity's room: its registry assignment, else its HA area.

    An assignment to None (Unassigned) still wins over the HA area. Both paths
    are in memory, since this runs on the event loop for every push.
    """
    registry = get_registry_engine(hass)
    if registry is not None:
        room = registry.room_of(entity_id)
        if room is not UNSET:
            return room
    return ha_area_id_of(hass, entity_id)


def area_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve an entity's room name (registry room first, HA area fallback)."""
    area_id = area_id_of(hass, entity_id)
    if area_id is None:
        return None
    registry = get_registry_engine(hass)
    if registry is not None:
        name = registry.room_name(area_id)
        if name is not None:
            return name
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def in_scope(hass: HomeAssistant, entity_id: str, rooms: list[str] | None) -> bool:
    """True when the entity is in one of the token's rooms.

    rooms is the token's rooms claim, None for an unrestricted token. An
    entity with no room is outside every scope.
    """
    if rooms is None:
        return True
    area_id = area_id_of(hass, entity_id)
    return area_id is not None and area_id in rooms


def is_visible(hass: HomeAssistant, entity_id: str) -> bool:
    """False for a hidden entity or a config/diagnostic entity the app skips.

    An entity with no registry entry is visible.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return True
    # HA may hide OpenWeatherMap's temperature and humidity sensors, but the
    # tablet shows them, so they are the one exception to hidden_by.
    if entry.hidden_by is not None and not is_openweathermap_measurement(
        hass, entity_id
    ):
        return False
    if entry.entity_category is None:
        return True
    state = hass.states.get(entity_id)
    device_class = (
        state.attributes.get("device_class") if state is not None else None
    ) or entry.original_device_class
    return is_category_served(str(entry.entity_category.value), entity_id, device_class)


def is_weather_service_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """True for a sensor whose device also has a weather entity.

    Such sensors belong to a weather service (OpenWeatherMap, Met.no) rather
    than a device in the home.
    """
    if not (entity_id.startswith("sensor.") or entity_id.startswith("binary_sensor.")):
        return False
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return False
    return any(
        sibling.entity_id.startswith("weather.")
        for sibling in er.async_entries_for_device(
            registry, entry.device_id, include_disabled_entities=True
        )
    )


def is_openweathermap_measurement(hass: HomeAssistant, entity_id: str) -> bool:
    """True for a live OpenWeatherMap temperature or humidity sensor.

    Matched on registry data. An entity without a state (disabled or missing)
    never matches.
    """
    if not entity_id.startswith("sensor."):
        return False
    state = hass.states.get(entity_id)
    if state is None:
        return False
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return False
    device_class = state.attributes.get("device_class") or entry.original_device_class
    if device_class not in {"temperature", "humidity"}:
        return False
    for sibling in er.async_entries_for_device(
        registry, entry.device_id, include_disabled_entities=True
    ):
        if not sibling.entity_id.startswith("weather.") or not sibling.config_entry_id:
            continue
        config_entry = hass.config_entries.async_get_entry(sibling.config_entry_id)
        if config_entry is not None and config_entry.domain == "openweathermap":
            return True
    return False


def is_served(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the app may see this entity at all.

    It needs an exposed domain and registry visibility, and weather-service
    sensors are left out except OpenWeatherMap's live readings. Device lists,
    pushes, commands, history and scene writes all use it.
    """
    weather_measurement = is_openweathermap_measurement(hass, entity_id)
    return (
        is_exposed(entity_id)
        and is_visible(hass, entity_id)
        and (weather_measurement or not is_weather_service_entity(hass, entity_id))
    )


def is_assignable(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the registry may assign, rename, reorder or clear an entity.

    Looser than is_served: an entity the integration hid after import (a
    secondary gang switch) must stay movable. It only has to exist and have
    an exposed domain.
    """
    if not is_exposed(entity_id):
        return False
    if er.async_get(hass).async_get(entity_id) is not None:
        return True
    return hass.states.get(entity_id) is not None


def device_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """The entity's HA device id, or None (the app groups tiles by it)."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.device_id if entry is not None else None


def serialize_device(hass: HomeAssistant, state: State) -> dict[str, Any]:
    """A state as the app's device dict, with room name and display name."""
    entry = er.async_get(hass).async_get(state.entity_id)
    device = serialize_state(
        state,
        area=area_name(hass, state.entity_id),
        entity_category=(
            str(entry.entity_category.value)
            if entry is not None and entry.entity_category is not None
            else None
        ),
    )
    device["device_id"] = device_id_of(hass, state.entity_id)
    registry = get_registry_engine(hass)
    if registry is not None:
        display_name = registry.display_name_of(state.entity_id)
        if display_name:
            device["name"] = display_name
    return device
