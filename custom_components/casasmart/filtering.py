"""Shared exposure filter (Track B — B1.4/B1.5): the ONE code path that
decides what crosses the API boundary.

Both the REST views and the WebSocket server route every outgoing device
through these functions (plan, audit round 5: "Every state push goes
through the exact same filter function REST uses — one shared code path
so WS can never leak what REST hides"). Do not duplicate this logic.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .entity_bridge import is_exposed, serialize_state


def area_id_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve an entity's area id (entity override first, then device)."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    area_id = entry.area_id
    if area_id is None and entry.device_id is not None:
        device = dr.async_get(hass).async_get(entry.device_id)
        area_id = device.area_id if device else None
    return area_id


def area_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve an entity's area name (entity override first, then device)."""
    area_id = area_id_of(hass, entity_id)
    if area_id is None:
        return None
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
    """True when the entity should appear in the device list.

    Filters out registry-hidden entities and config/diagnostic entities
    (firmware sensors, restart buttons, ...) — the app shows devices,
    not plumbing. Entities with no registry entry (template/MQTT-yaml)
    are visible by default.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return True
    return entry.hidden_by is None and entry.entity_category is None


def is_served(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity is part of the CasaSmart API surface at all:
    exposed domain AND visible. The single gate for REST and WS alike."""
    return is_exposed(entity_id) and is_visible(hass, entity_id)


def serialize_device(hass: HomeAssistant, state: State) -> dict[str, Any]:
    """Serialize a state into the wire device dict, area resolved."""
    return serialize_state(state, area=area_name(hass, state.entity_id))
