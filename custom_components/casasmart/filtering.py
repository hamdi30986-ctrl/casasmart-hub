"""Shared exposure filter (Track B — B1.4/B1.5): the ONE code path that
decides what crosses the API boundary.

Both the REST views and the WebSocket server route every outgoing device
through these functions (plan, audit round 5: "Every state push goes
through the exact same filter function REST uses — one shared code path
so WS can never leak what REST hides"). Do not duplicate this logic.
"""

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
    """Resolve an entity's room (B17: registry first, HA area fallback).

    A registry assignment is the installer's word and wins outright —
    including an explicit ``None`` ("Unassigned"), which must NOT snap
    back to the HA area. Only entities with no registry record at all
    fall through to HA's own area registry. Pure in-memory on both
    paths — this runs on the event loop for every pushed state change.
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


def in_scope(
    hass: HomeAssistant, entity_id: str, rooms: list[str] | None
) -> bool:
    """Room-scope check (B1.6): is this entity inside the token's scope?

    ``rooms`` is the JWT's ``rooms`` claim — a list of area ids, or None
    for an unrestricted token (admin/sub-admin and unscoped users).
    Entities with NO area are invisible to room-scoped tokens: scoping is
    a restriction, and "unassigned" is not a room anyone was granted.
    """
    if rooms is None:
        return True
    area_id = area_id_of(hass, entity_id)
    return area_id is not None and area_id in rooms


def is_visible(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity belongs on the CasaSmart feed.

    Registry-hidden entities never cross. Category entities follow the
    shared policy in ``entity_bridge.is_category_served`` (B16 3c-4a):
    config entities are served (the app's settings sheets read and drive
    them — LED mode, child lock, power-outage memory), and diagnostic
    SENSORS with a curated measurement device_class are served (energy
    panel voltage/current, device temperature). Everything else with a
    category (linkquality chatter, firmware plumbing) stays hub-internal.
    Entities with no registry entry (template/MQTT-yaml) are visible by
    default.
    """
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


def is_served(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity is part of the CasaSmart API surface at all:
    exposed domain AND visible. The single gate for REST and WS alike."""
    return is_exposed(entity_id) and is_visible(hass, entity_id)


def device_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """HA device-registry id for an entity, or None when it has no
    registry entry / no parent device (template/MQTT-yaml entities).

    The app groups multi-entity hardware into one tile by this id —
    it is an opaque grouping key on the wire, never an HA handle the
    app can act on (commands stay entity_id + whitelisted action)."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.device_id if entry is not None else None


def serialize_device(hass: HomeAssistant, state: State) -> dict[str, Any]:
    """Serialize a state into the wire device dict — area resolved,
    the installer's registry display name (B17) overriding the HA
    friendly name when one is set, and the HA device-registry id as
    the app's tile-grouping key (B16)."""
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
