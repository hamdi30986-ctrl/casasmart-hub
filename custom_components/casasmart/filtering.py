"""CasaSmart runtime component."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .const import DOMAIN
from .entity_bridge import is_category_served, is_exposed, serialize_state
from .registry import UNSET

if TYPE_CHECKING:
    from .registry import RegistryEngine


def get_registry_engine(hass: HomeAssistant) -> "RegistryEngine | None":
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    return entries[0].runtime_data.registry


def ha_area_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """CasaSmart runtime component."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    area_id = entry.area_id
    if area_id is None and entry.device_id is not None:
        device = dr.async_get(hass).async_get(entry.device_id)
        area_id = device.area_id if device else None
    return area_id


def area_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """CasaSmart runtime component."""
    registry = get_registry_engine(hass)
    if registry is not None:
        room = registry.room_of(entity_id)
        if room is not UNSET:
            return room
    return ha_area_id_of(hass, entity_id)


def area_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """CasaSmart runtime component."""
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


def in_scope(
    hass: HomeAssistant, entity_id: str, rooms: list[str] | None
) -> bool:
    """CasaSmart runtime component."""
    if rooms is None:
        return True
    area_id = area_id_of(hass, entity_id)
    return area_id is not None and area_id in rooms


def is_visible(hass: HomeAssistant, entity_id: str) -> bool:
    """CasaSmart runtime component."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return True
    if entry.hidden_by is not None:
        return False
    if entry.entity_category is None:
        return True
    state = hass.states.get(entity_id)
    device_class = (
        state.attributes.get("device_class") if state is not None else None
    ) or entry.original_device_class
    return is_category_served(
        str(entry.entity_category.value), entity_id, device_class
    )


def is_weather_service_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """CasaSmart runtime component."""
    if not (
        entity_id.startswith("sensor.")
        or entity_id.startswith("binary_sensor.")
    ):
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


def is_served(hass: HomeAssistant, entity_id: str) -> bool:
    """CasaSmart runtime component."""
    return (
        is_exposed(entity_id)
        and is_visible(hass, entity_id)
        and not is_weather_service_entity(hass, entity_id)
    )


def is_assignable(hass: HomeAssistant, entity_id: str) -> bool:
    """CasaSmart runtime component."""
    if not is_exposed(entity_id):
        return False
    if er.async_get(hass).async_get(entity_id) is not None:
        return True
    return hass.states.get(entity_id) is not None


def device_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """CasaSmart runtime component."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.device_id if entry is not None else None


def serialize_device(hass: HomeAssistant, state: State) -> dict[str, Any]:
    """CasaSmart runtime component."""
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
