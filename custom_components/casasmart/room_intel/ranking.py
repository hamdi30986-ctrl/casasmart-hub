"""Room ranking: the caller's rooms, busiest first.

A room's activity is how many of its safe devices are on: lights, fans, and
switches whose gang is a light, fan, switch or outlet. Only devices imported
into the registry count. When any of the caller's rooms takes part in room
commands, only those rooms and the devices their policies approve count;
otherwise every visible room does. Rooms sort by that count, most first, then
by room id. The Now page and the generated room suggestions use this one
ranking (docs/api/GENERATED_ROOM_SUGGESTIONS_V1.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from ..filtering import area_id_of, in_scope, is_served
from ..now_data import is_room_activity_candidate

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..now_data import NowDataEngine
    from ..registry import RegistryEngine

# Older registry records key gang types by these channel names instead of by
# entity id.
_LEGACY_GANG_SUFFIXES = (
    "left",
    "right",
    "center",
    "l1",
    "l2",
    "l3",
    "endpoint_1",
    "endpoint_2",
    "endpoint_3",
    "gang_1",
    "gang_2",
    "gang_3",
)
# The gang types that make a switch count as room activity.
_ACTIVE_SWITCH_GANG_TYPES = frozenset({"light", "fan", "switch", "outlet"})


class RoomRanking(NamedTuple):
    """The ranked rooms, and the states they were ranked from."""

    rooms: list[dict[str, Any]]
    # Each scoped room's imported, visible control states.
    states_by_room: dict[str, list[Any]]
    # Every imported, visible control state, by entity id.
    states: dict[str, Any]


def imported_controls(devices: list[dict[str, Any]]) -> dict[str, str | None]:
    """Each imported control entity of the registry's devices, with its gang type.

    A device's config entities and hidden gangs are left out. The gang type is
    None for a control without one.
    """
    gang_types: dict[str, str | None] = {}
    for device in devices:
        gangs = device.get("gangs", {})
        legacy_types = device.get("gang_types", {})
        config_ids = set(device.get("config_entity_ids", []))
        for eid in device.get("control_entity_ids", device.get("entity_ids", [])):
            gang = gangs.get(eid, {})
            if eid in config_ids or gang.get("presentation") == "hidden":
                continue
            local = eid.split(".", 1)[-1]
            key = next(
                (
                    s
                    for s in _LEGACY_GANG_SUFFIXES
                    if local == s or local.endswith("_" + s)
                ),
                eid,
            )
            gang_types[eid] = gang.get("type", legacy_types.get(key))
    return gang_types


def counts_as_activity(entity_id: str, gang_type: str | None) -> bool:
    """True for a light, a fan, or a switch whose gang is a light, fan, switch
    or outlet."""
    domain = entity_id.split(".")[0]
    return domain in {"light", "fan"} or (
        domain == "switch" and gang_type in _ACTIVE_SWITCH_GANG_TYPES
    )


def rank_rooms(
    rooms: list[dict[str, Any]],
    policies: dict[str, dict[str, Any]],
    states_by_room: dict[str, list[Any]],
    gang_types: dict[str, str | None],
) -> list[dict[str, Any]]:
    """Rank the (already scoped) rooms by how many safe devices are on.

    Each ranked room is its registry record plus active_count and the
    eligible_entity_ids that were counted.
    """
    configured = any(policies.get(r["room_id"], {}).get("participates") for r in rooms)
    ranked = []
    for room in rooms:
        rid = room["room_id"]
        policy = policies.get(rid, {})
        if configured and not policy.get("participates"):
            continue
        eligible = []
        for state in states_by_room[rid]:
            eid = state.entity_id
            if not counts_as_activity(eid, gang_types.get(eid)) or state.state in {
                "unknown",
                "unavailable",
            }:
                continue
            if configured and (
                eid not in policy.get("eligible_entity_ids", [])
                or not is_room_activity_candidate(state)
            ):
                continue
            eligible.append(state)
        count = sum(state.state == "on" for state in eligible)
        if count or configured:
            ranked.append(
                {
                    **room,
                    "active_count": count,
                    "eligible_entity_ids": [state.entity_id for state in eligible],
                    "restore_pending_count": 0,
                    "most_recent_activity_at": None,
                }
            )
    ranked.sort(key=lambda r: (-r["active_count"], r["room_id"]))
    return ranked


async def async_rank_rooms(
    hass: HomeAssistant,
    registry: RegistryEngine,
    now_data: NowDataEngine,
    scope: list[str] | None,
) -> RoomRanking:
    """Rank the caller's rooms; scope is their room ids, None for every room.

    The stores are read in one executor job and the states in one scan.
    """

    def read_stores():
        rooms = registry.list_rooms()
        policies = {r["room_id"]: now_data.room_policy(r["room_id"]) for r in rooms}
        return rooms, policies, registry.list_user_devices()

    rooms, policies, devices = await hass.async_add_executor_job(read_stores)
    rooms = [r for r in rooms if scope is None or r["room_id"] in scope]
    gang_types = imported_controls(devices)
    states = {
        s.entity_id: s
        for s in hass.states.async_all()
        if s.entity_id in gang_types
        and is_served(hass, s.entity_id)
        and in_scope(hass, s.entity_id, scope)
    }
    states_by_room: dict[str, list[Any]] = {r["room_id"]: [] for r in rooms}
    for state in states.values():
        rid = area_id_of(hass, state.entity_id)
        if rid in states_by_room:
            states_by_room[rid].append(state)
    return RoomRanking(
        rank_rooms(rooms, policies, states_by_room, gang_types),
        states_by_room,
        states,
    )
