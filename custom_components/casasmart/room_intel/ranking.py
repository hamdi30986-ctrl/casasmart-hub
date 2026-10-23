"""Room ranking: the caller's rooms, the ones with something on first.

A room's on devices are its safe devices that are on (lights, fans, and
switches whose gang is a light, fan, switch or outlet; that number is
active_count) plus every AC that is on. Only devices imported into the
registry count. When any of the caller's rooms takes part in room commands,
only those rooms and the safe devices their policies approve count (an AC
counts in every listed room: a policy can't approve one); otherwise every
visible room does.

Rooms with something on come first. When every one of them is fully metered
(each on device's own power meters read valid), they rank by watts;
otherwise the whole list ranks by how many devices are on. Ties go to the
room order the registry serves the apps: sort_order, then name, then room
id. Off rooms fill the Now page's empty card slots in order_off_rooms order.
The Now page and the generated room suggestions use this one ranking
(docs/api/NOW_ROOM_RANKING_V1.md, GENERATED_ROOM_SUGGESTIONS_V1.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from ..filtering import area_id_of, in_scope, is_served
from ..now_data import is_room_activity_candidate
from .meters import is_power_meter, watt_step, watts_of

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
# The HVAC modes an AC is on in: every mode but off, as
# generated_suggestions.room_actions has it.
_AC_ON_MODES = frozenset({"cool", "heat", "auto", "heat_cool", "dry", "fan_only"})
_UNAVAILABLE = frozenset({"unknown", "unavailable"})
# The Now page's room cards: a featured one and four more
# (now_data.room_activity_layout; the tablet's lead card and four). Off rooms
# fill the ones on rooms leave empty.
NOW_ROOM_SLOTS = 5
RANKING_VERSION = 1


class RoomRanking(NamedTuple):
    """The ranked rooms, and the states they were ranked from."""

    # On rooms, then the off rooms that fill empty slots, then (in a home
    # with room policies) every other participating room.
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


def device_keys(
    devices: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Which registry device each control, and each sensor, belongs to.

    Returns (control entity id -> device key, sensor entity id -> device
    key); a device's key is its position in the list (one record per Home
    Assistant device). A device's sensors are its control and config
    entities in the sensor domain: the candidates for its power meters.
    """
    controls: dict[str, int] = {}
    sensors: dict[str, int] = {}
    for key, device in enumerate(devices):
        control_ids = device.get("control_entity_ids", device.get("entity_ids", []))
        for eid in control_ids:
            controls.setdefault(eid, key)
        for eid in (*control_ids, *device.get("config_entity_ids", [])):
            if eid.startswith("sensor."):
                sensors.setdefault(eid, key)
    return controls, sensors


def counts_as_activity(entity_id: str, gang_type: str | None) -> bool:
    """True for a light, a fan, or a switch whose gang is a light, fan, switch
    or outlet."""
    domain = entity_id.split(".")[0]
    return domain in {"light", "fan"} or (
        domain == "switch" and gang_type in _ACTIVE_SWITCH_GANG_TYPES
    )


def is_ac(state: Any) -> bool:
    """True for a cooling-capable climate entity not excluded from room
    activity (generated_suggestions.room_actions' test for an AC)."""
    attributes = state.attributes
    return (
        state.entity_id.startswith("climate.")
        and "cool" in (attributes.get("hvac_modes") or [])
        and attributes.get("casasmart_room_activity_exclude") is not True
    )


def room_order_key(room: dict[str, Any]) -> tuple[int, str, str]:
    """The registry's room order: sort_order, then name, then room id."""
    sort_order = room.get("sort_order")
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        sort_order = 0
    return sort_order, str(room.get("name") or ""), room["room_id"]


def order_off_rooms(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the rooms with nothing on, for the slots on rooms leave empty.

    The room order for now. The all-off ranking replaces this function;
    rank_rooms takes the off rooms in whatever order it returns.
    """
    return sorted(rooms, key=room_order_key)


def ranking_basis(rooms: list[dict[str, Any]]) -> str:
    """What the on rooms rank by: "watts" when a room is on and every on
    room is fully metered, else "count" (for the whole list)."""
    on = [room["rank"] for room in rooms if room["rank"]["active"]]
    if on and all(rank["watts_quality"] == "complete" for rank in on):
        return "watts"
    return "count"


def ranking_summary(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    """How the ranked rooms were ordered, for the Now page."""
    return {
        "version": RANKING_VERSION,
        "basis": ranking_basis(rooms),
        "slots": NOW_ROOM_SLOTS,
    }


def _room_watts(
    room_meters: dict[int, list[Any]],
    devices: list[int | None],
    on_devices: list[int | None],
) -> tuple[float | None, str]:
    """A room's watts and how complete they are.

    room_meters holds each device's meters in the room; devices are the keys
    of the room's counted devices, on_devices of those that are on. An on
    device is covered when its device has a meter in the room and every one
    of them reads valid. Quality is "absent" with no valid reading,
    "complete" when every on device is covered, else "partial".
    """
    readings = {
        meter.entity_id: watts_of(meter)
        for key in dict.fromkeys(devices)
        for meter in room_meters.get(key, ())
    }
    valid = [watts for watts in readings.values() if watts is not None]
    if not valid:
        return None, "absent"

    def covered(key: int | None) -> bool:
        meters = room_meters.get(key, ())
        return bool(meters) and all(
            readings[meter.entity_id] is not None for meter in meters
        )

    complete = all(covered(key) for key in on_devices)
    return round(sum(valid), 1), "complete" if complete else "partial"


def _count_key(room: dict[str, Any]) -> tuple:
    return (-room["rank"]["on_count"], *room_order_key(room))


def _watts_key(room: dict[str, Any]) -> tuple:
    rank = room["rank"]
    return (-watt_step(rank["watts"]), -rank["on_count"], *room_order_key(room))


def rank_rooms(
    rooms: list[dict[str, Any]],
    policies: dict[str, dict[str, Any]],
    states_by_room: dict[str, list[Any]],
    gang_types: dict[str, str | None],
    meters_by_room: dict[str, dict[int, list[Any]]],
    device_of: dict[str, int],
) -> list[dict[str, Any]]:
    """Rank the (already scoped) rooms, the ones with something on first.

    meters_by_room holds, per room, each device's power meters in that room;
    device_of maps a control to its device. Each ranked room is its registry
    record plus active_count, the eligible_entity_ids that were counted and
    a rank block: active, filler, on_count, ac_on_count, watts and
    watts_quality.
    """
    configured = any(policies.get(r["room_id"], {}).get("participates") for r in rooms)
    on_rooms = []
    off_rooms = []
    fill_ids = set()
    for room in rooms:
        rid = room["room_id"]
        policy = policies.get(rid, {})
        if configured and not policy.get("participates"):
            continue
        eligible = []
        acs = []
        for state in states_by_room[rid]:
            eid = state.entity_id
            if state.state in _UNAVAILABLE:
                continue
            if is_ac(state):
                acs.append(state)
                continue
            if not counts_as_activity(eid, gang_types.get(eid)):
                continue
            if configured and (
                eid not in policy.get("eligible_entity_ids", [])
                or not is_room_activity_candidate(state)
            ):
                continue
            eligible.append(state)
        on = [state for state in eligible if state.state == "on"]
        acs_on = [state for state in acs if state.state in _AC_ON_MODES]
        watts, quality = _room_watts(
            meters_by_room.get(rid, {}),
            [device_of.get(state.entity_id) for state in [*eligible, *acs]],
            [device_of.get(state.entity_id) for state in [*on, *acs_on]],
        )
        ranked = {
            **room,
            "active_count": len(on),
            "eligible_entity_ids": [state.entity_id for state in eligible],
            "restore_pending_count": 0,
            "most_recent_activity_at": None,
            "rank": {
                "active": bool(on or acs_on),
                "filler": False,
                "on_count": len(on) + len(acs_on),
                "ac_on_count": len(acs_on),
                "watts": watts,
                "watts_quality": quality,
            },
        }
        if ranked["rank"]["active"]:
            on_rooms.append(ranked)
            continue
        off_rooms.append(ranked)
        # Only a room with a device to switch fills a slot.
        if eligible or acs:
            fill_ids.add(rid)
    on_rooms.sort(key=_watts_key if ranking_basis(on_rooms) == "watts" else _count_key)
    off_rooms = order_off_rooms(off_rooms)
    fillers = [r for r in off_rooms if r["room_id"] in fill_ids]
    fillers = fillers[: max(0, NOW_ROOM_SLOTS - len(on_rooms))]
    for room in fillers:
        room["rank"]["filler"] = True
    if not configured:
        return on_rooms + fillers
    # Every participating room stays listed, as room commands need them.
    return on_rooms + fillers + [r for r in off_rooms if not r["rank"]["filler"]]


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
    device_of, sensor_device = device_keys(devices)
    states = {}
    meters = []
    for state in hass.states.async_all():
        eid = state.entity_id
        control = eid in gang_types
        meter = eid in sensor_device and is_power_meter(state)
        if (
            not (control or meter)
            or not is_served(hass, eid)
            or not in_scope(hass, eid, scope)
        ):
            continue
        if control:
            states[eid] = state
        if meter:
            meters.append(state)
    states_by_room: dict[str, list[Any]] = {r["room_id"]: [] for r in rooms}
    for state in states.values():
        rid = area_id_of(hass, state.entity_id)
        if rid in states_by_room:
            states_by_room[rid].append(state)
    meters_by_room: dict[str, dict[int, list[Any]]] = {}
    for meter in meters:
        rid = area_id_of(hass, meter.entity_id)
        if rid in states_by_room:
            meters_by_room.setdefault(rid, {}).setdefault(
                sensor_device[meter.entity_id], []
            ).append(meter)
    return RoomRanking(
        rank_rooms(
            rooms, policies, states_by_room, gang_types, meters_by_room, device_of
        ),
        states_by_room,
        states,
    )
