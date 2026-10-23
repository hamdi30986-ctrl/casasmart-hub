"""Room ranking: the caller's rooms, the ones with something on first.

A room's on devices are its safe devices that are on (lights, fans, and
switches whose gang is a light, fan, switch or outlet; that number is
active_count) plus every AC that is on. Only devices imported into the
registry count. When an admin's stored policy makes any of the caller's rooms
take part in room commands, only the rooms that take part count, with the
devices their policies approve: a stored policy's list, or a room without
one's lights and fans (wall relays typed as one included; an AC counts in
every listed room: a policy can't approve one); otherwise every visible room
does.

Rooms with something on come first. When every one of them is fully metered
(each on device's own power meters read valid), they rank by watts;
otherwise the whole list ranks by how many devices are on. Ties go to the
room order the registry serves the apps: sort_order, then name, then room
id. Off rooms fill the Now page's empty card slots in the all-off order
(order_off_rooms, tiers in room_intel.off_rooms): something still running,
then just went dark, then usually on around now (room_intel.habits), then
the rooms the viewer switched off themselves, then room order. Every room
says why it is where it is (rank.reason). The Now page and the generated room
suggestions use this one ranking (docs/api/NOW_ROOM_RANKING_V1.md,
GENERATED_ROOM_SUGGESTIONS_V1.md); only the Now page passes the room activity
log, which just_off needs, and the habits.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Container, Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from ..filtering import area_id_of, in_scope, is_served
from ..now_data import is_default_room_device, is_room_activity_candidate
from .meters import is_power_meter, watt_step, watts_of
from .off_rooms import (
    JUST_OFF_WINDOW_SECONDS,
    LEARNED_OWN_OFF,
    REASON_ACTIVE,
    REASON_JUST_OFF,
    REASON_ROOM_ORDER,
    REASON_RUNNING_LOAD,
    TIER_JUST_OFF,
    TIER_LEARNED,
    TIER_ROOM_ORDER,
    TIER_RUNNING_LOAD,
    OffRoomFacts,
    RecentActivity,
    changed_at,
    events_by_room,
    is_playing,
    iso_utc,
    still_running,
    went_dark,
)

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
MODE_ON = "on"
MODE_ALL_OFF = "all_off"

# A learned all-off tier: the room's (reason, params, in-tier sort key),
# or None when it doesn't qualify. The key starts with the sub-slot
# (off_rooms.LEARNED_HABIT, ...).
LearnedTier = Callable[[dict[str, Any]], tuple[str, dict[str, Any], tuple] | None]


class ActivitySource(Protocol):
    """What the ranking reads the room activity log through (the recorder)."""

    started_at: float | None

    async def async_recent_events(
        self, since_t: int, room_ids: list[str]
    ) -> list[dict[str, Any]]: ...

    def held_meters(self) -> frozenset[str]:
        """The power meters holding a running load (off_rooms.LoadLatch)."""
        ...


class HabitSource(Protocol):
    """What the ranking asks the room habits through (learner.HabitQuery)."""

    async def async_learned(
        self, room_ids: list[str], counted: frozenset[str], now_t: float | None
    ) -> LearnedTier | None: ...


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


def approved_by(
    policy: Mapping[str, Any], state: Any, gang_type: str | None = None
) -> bool:
    """True when the room's policy lets room commands switch the device: on
    its stored policy's list (and a candidate), or, in a room without a
    stored policy, one of its default devices (now_data.room_policy).
    gang_type is the device's registry gang type (a wall relay typed as a
    light or fan is a light or fan)."""
    if policy.get("default"):
        return is_default_room_device(state, gang_type)
    return state.entity_id in policy.get(
        "eligible_entity_ids", []
    ) and is_room_activity_candidate(state, gang_type)


def is_ac(state: Any) -> bool:
    """True for a cooling-capable climate entity not excluded from room
    activity (generated_suggestions.room_actions' test for an AC)."""
    attributes = state.attributes
    return (
        state.entity_id.startswith("climate.")
        and "cool" in (attributes.get("hvac_modes") or [])
        and attributes.get("casasmart_room_activity_exclude") is not True
    )


def counted_controls(
    gang_types: Mapping[str, str | None], state_of: Callable[[str], Any]
) -> frozenset[str]:
    """The imported controls that make their room on when they are on.

    Safe controls (counts_as_activity) and ACs (is_ac, judged from the
    state: an unavailable AC keeps its hvac_modes), wherever they are and
    whatever room policies say. The habits learn from these devices' "on"s.
    """
    counted = set()
    for entity_id, gang in gang_types.items():
        if counts_as_activity(entity_id, gang):
            counted.add(entity_id)
        elif entity_id.startswith("climate."):
            state = state_of(entity_id)
            if state is not None and is_ac(state):
                counted.add(entity_id)
    return frozenset(counted)


async def async_counted_controls(
    hass: HomeAssistant, registry: RegistryEngine
) -> tuple[frozenset[str], frozenset[str]]:
    """(counted_controls, every imported control) from the registry now."""
    devices = await hass.async_add_executor_job(registry.list_user_devices)
    gang_types = imported_controls(devices)
    return counted_controls(gang_types, hass.states.get), frozenset(gang_types)


def room_order_key(room: dict[str, Any]) -> tuple[int, str, str]:
    """The registry's room order: sort_order, then name, then room id."""
    sort_order = room.get("sort_order")
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        sort_order = 0
    return sort_order, str(room.get("name") or ""), room["room_id"]


def _off_tier(
    room: dict[str, Any], facts: OffRoomFacts, learned: LearnedTier | None
) -> tuple[int, str, dict[str, Any], tuple]:
    """An off room's tier: (sort slot, reason, params, in-tier sort key)."""
    if facts.loads:
        step = -2 if facts.watts is None else watt_step(facts.watts)
        return TIER_RUNNING_LOAD, REASON_RUNNING_LOAD, {"loads": facts.loads}, (-step,)
    if facts.went_dark_t is not None:
        return (
            TIER_JUST_OFF,
            REASON_JUST_OFF,
            {"went_dark_at": iso_utc(facts.went_dark_t)},
            (-facts.went_dark_t,),
        )
    if facts.own_off_t is not None:
        # The viewer's own off: below the habits, never one of them.
        return (
            TIER_LEARNED,
            REASON_JUST_OFF,
            {"went_dark_at": iso_utc(facts.own_off_t), "by_you": True},
            (LEARNED_OWN_OFF, -facts.own_off_t),
        )
    if learned is not None and (tier := learned(room)) is not None:
        reason, params, key = tier
        return TIER_LEARNED, reason, params, key
    return TIER_ROOM_ORDER, REASON_ROOM_ORDER, {}, ()


def order_off_rooms(
    rooms: list[dict[str, Any]],
    facts: Mapping[str, OffRoomFacts],
    learned: LearnedTier | None = None,
) -> list[dict[str, Any]]:
    """Order the rooms with nothing on, and give each its rank reason.

    Tiers (room_intel.off_rooms), the first a room qualifies for wins:
    running_load, just_off, the viewer's own off, learned (habit_now), then
    room_order; the viewer's own offs sort after the habits. Ties and the
    last tier go by room order. learned is the habits' hook (None: no
    habits). rank_rooms fills the empty slots from the front of this order.
    """
    keyed = []
    for room in rooms:
        slot, reason, params, key = _off_tier(room, facts[room["room_id"]], learned)
        room["rank"]["reason"] = reason
        room["rank"]["params"] = params
        keyed.append(((slot, key, room_order_key(room)), room))
    keyed.sort(key=lambda item: item[0])
    return [room for _, room in keyed]


def ranking_basis(rooms: list[dict[str, Any]]) -> str:
    """What the on rooms rank by: "watts" when a room is on and every on
    room is fully metered, else "count" (for the whole list)."""
    on = [room["rank"] for room in rooms if room["rank"]["active"]]
    if on and all(rank["watts_quality"] == "complete" for rank in on):
        return "watts"
    return "count"


def ranking_summary(
    rooms: list[dict[str, Any]], habits: dict[str, str] | None = None
) -> dict[str, Any]:
    """How the ranked rooms were ordered, for the Now page.

    mode is "on" when a room is on, else "all_off" (the whole list is in
    the all-off order). habits, when given, says which habit layer ranked
    and how far learning is (learner.HabitQuery.summary).
    """
    summary = {
        "version": RANKING_VERSION,
        "basis": ranking_basis(rooms),
        "slots": NOW_ROOM_SLOTS,
        "mode": MODE_ON
        if any(room["rank"]["active"] for room in rooms)
        else MODE_ALL_OFF,
    }
    if habits is not None:
        summary["habits"] = dict(habits)
    return summary


def _room_watts(
    room_meters: dict[int, list[Any]],
    devices: list[int | None],
    on_devices: list[int | None],
) -> tuple[float | None, str, dict[str, float]]:
    """A room's watts, how complete they are, and its valid readings.

    room_meters holds each device's meters in the room; devices are the keys
    of the room's counted devices, on_devices of those that are on. An on
    device is covered when its device has a meter in the room and every one
    of them reads valid. Quality is "absent" with no valid reading,
    "complete" when every on device is covered, else "partial". The valid
    readings are by meter entity id.
    """
    readings = {
        meter.entity_id: watts_of(meter)
        for key in dict.fromkeys(devices)
        for meter in room_meters.get(key, ())
    }
    valid = {eid: watts for eid, watts in readings.items() if watts is not None}
    if not valid:
        return None, "absent", {}

    def covered(key: int | None) -> bool:
        meters = room_meters.get(key, ())
        return bool(meters) and all(
            readings[meter.entity_id] is not None for meter in meters
        )

    complete = all(covered(key) for key in on_devices)
    quality = "complete" if complete else "partial"
    return round(sum(valid.values()), 1), quality, valid


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
    *,
    activity: RecentActivity | None = None,
    learned: LearnedTier | None = None,
    held_meters: Container[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Rank the (already scoped) rooms, the ones with something on first.

    meters_by_room holds, per room, each device's power meters in that room;
    device_of maps a control to its device. activity is the room activity
    the just_off tier needs; without it no room is just_off. learned is the
    habit tier (None: no room is habit_now). held_meters are the meters
    holding a running load (off_rooms.LoadLatch; none: a meter runs from
    RUNNING_LOAD_MIN_W only). Each ranked
    room is its registry record plus active_count, the eligible_entity_ids
    that were counted and a rank block: active, filler, on_count,
    ac_on_count, watts, watts_quality, reason and params.
    """
    # A room without a stored policy takes part too (its policy is the
    # default), but only an admin's stored one switches the list over.
    configured = any(
        policy.get("participates") and not policy.get("default")
        for policy in (policies.get(r["room_id"], {}) for r in rooms)
    )
    events = events_by_room(activity.events) if activity is not None else {}
    on_rooms = []
    off_rooms = []
    facts: dict[str, OffRoomFacts] = {}
    fill_ids = set()
    for room in rooms:
        rid = room["room_id"]
        policy = policies.get(rid, {})
        if configured and not policy.get("participates"):
            continue
        eligible = []
        acs = []
        # Every device that counts the room as on, available or not, and
        # when each unavailable one got so (for just_off).
        counted_ids = set()
        unavailable_since = []
        media_playing = 0
        for state in states_by_room[rid]:
            eid = state.entity_id
            if is_playing(state):
                media_playing += 1
                continue
            if is_ac(state):
                kind = acs
            elif counts_as_activity(eid, gang_types.get(eid)) and not (
                configured and not approved_by(policy, state, gang_types.get(eid))
            ):
                kind = eligible
            else:
                continue
            counted_ids.add(eid)
            if state.state in _UNAVAILABLE:
                unavailable_since.append(changed_at(state))
                continue
            kind.append(state)
        on = [state for state in eligible if state.state == "on"]
        acs_on = [state for state in acs if state.state in _AC_ON_MODES]
        watts, quality, readings = _room_watts(
            meters_by_room.get(rid, {}),
            [device_of.get(state.entity_id) for state in [*eligible, *acs]],
            [device_of.get(state.entity_id) for state in [*on, *acs_on]],
        )
        active = bool(on or acs_on)
        ranked = {
            **room,
            "active_count": len(on),
            "eligible_entity_ids": [state.entity_id for state in eligible],
            "restore_pending_count": 0,
            "most_recent_activity_at": None,
            "rank": {
                "active": active,
                "filler": False,
                "on_count": len(on) + len(acs_on),
                "ac_on_count": len(acs_on),
                "watts": watts,
                "watts_quality": quality,
                "reason": REASON_ACTIVE,
                "params": {},
            },
        }
        if active:
            on_rooms.append(ranked)
            continue
        loads = still_running(media_playing, readings, held_meters)
        dark = (
            went_dark(events.get(rid, ()), counted_ids, unavailable_since, activity)
            if activity is not None
            else None
        )
        facts[rid] = OffRoomFacts(
            loads,
            watts,
            dark.t if dark is not None and not dark.by_viewer else None,
            dark.t if dark is not None and dark.by_viewer else None,
        )
        off_rooms.append(ranked)
        # Only a room with a device to switch, or something still running,
        # fills a slot.
        if eligible or acs or loads:
            fill_ids.add(rid)
    on_rooms.sort(key=_watts_key if ranking_basis(on_rooms) == "watts" else _count_key)
    off_rooms = order_off_rooms(off_rooms, facts, learned)
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
    *,
    activity: ActivitySource | None = None,
    member_id: str | None = None,
    now_t: float | None = None,
    habits: HabitSource | None = None,
    stores: tuple[list[dict[str, Any]], dict[str, Any], list[Any]] | None = None,
) -> RoomRanking:
    """Rank the caller's rooms; scope is their room ids, None for every room.

    activity is the room activity log (the recorder), for the just_off tier
    and the meters holding a running load; member_id is the caller, whose
    own app-tap offs sort below the habits.
    habits answers the habit tier for the caller's rooms (a
    learner.HabitQuery; None: no habits). The stores are read in one
    executor job, the states in one scan and the log in one query; the
    habits are served from memory (read from the log once a day). stores,
    when the caller already read them in its own executor job, are
    (registry.list_rooms(), each room's now_data.room_policy,
    registry.list_user_devices()), unscoped.
    """

    def read_stores():
        rooms = registry.list_rooms()
        policies = {r["room_id"]: now_data.room_policy(r["room_id"]) for r in rooms}
        return rooms, policies, registry.list_user_devices()

    rooms, policies, devices = (
        stores if stores is not None else await hass.async_add_executor_job(read_stores)
    )
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
    recent = None
    now_t = time.time() if now_t is None else now_t
    if activity is not None:
        recent = RecentActivity(
            await activity.async_recent_events(
                max(0, int(now_t) - JUST_OFF_WINDOW_SECONDS), list(states_by_room)
            ),
            now_t,
            member_id,
            activity.started_at,
        )
    learned = None
    if habits is not None:
        learned = await habits.async_learned(
            list(states_by_room),
            counted_controls(gang_types, hass.states.get),
            now_t,
        )
    return RoomRanking(
        rank_rooms(
            rooms,
            policies,
            states_by_room,
            gang_types,
            meters_by_room,
            device_of,
            activity=recent,
            learned=learned,
            held_meters=activity.held_meters() if activity is not None else (),
        ),
        states_by_room,
        states,
    )
