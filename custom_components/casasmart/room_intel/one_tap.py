"""One tap "on" for a room that is off: what it turns on, and from what.

One tap turns on the lights and fans that were
on last time, and an AC only from a fresh capture of the hub's own room off,
never plugs, heaters or covers. A wall relay (a switch)
counts as a light or fan when the installer typed it so
(now_data.lighting_kind). The room command and the
Now page ask the same question (plan_room), so the card says what the tap
does. The sources, first one with a device that can come on wins:

1. capture: what the hub's own room off switched off, if that was at most
   CAPTURE_MAX_AGE_SECONDS ago and the room hasn't been lit and gone dark
   again since (now_data's restore set), with its ACs at the mode and target
   they had (their steps: now_data.ac_restore_steps);
2. last_on: the lights and fans that were on just before the room last went
   dark, whoever switched it off: a wall switch, Home Assistant, an
   automation or the hub (LastOnSets, kept by room_intel.recorder);
3. start_profile: the lights and fans an installer chose for the room;
4. none: there is nothing to turn on, and the app opens the room instead.

Only lights and fans ever come on from a set, whatever a source holds
(ON_DOMAINS, and a switch only when the caller names it a typed wall relay:
kinds). An AC comes on only as the capture's, so never at a guess.

LastOnSets follows the state changes of the lights and fans the hub manages,
in the order Home Assistant fires them, once the recorder is ready (10 minutes
after a start, at once on a reload), so restored states and devices catching
up after a restart never replace a set. A room is lit while one of them is on.
When an on -> off takes the last one, the room went dark: its set is that
device plus the room's devices that went off in the OFF_SEQUENCE_SECONDS
before it, newest first (the gangs of a wall plate pressed one by one; a hub
room off also reports one device at a time). A room that goes dark any other
way (its last lit device goes unavailable, is removed, or moves room) keeps
the set it had: nothing new is known. A set holds no member id: it is the
room's, not anyone's.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, NamedTuple

# The only domains one tap turns on by themselves.
ON_DOMAINS = frozenset({"light", "fan"})
# A switch comes on only as a wall relay the installer typed as a light or
# fan (now_data.lighting_kind), and only when the caller names it so in
# kinds; a plug, outlet, heater or untyped switch never does.
RELAY_DOMAIN = "switch"
SOURCE_CAPTURE = "capture"
SOURCE_LAST_ON = "last_on"
SOURCE_START_PROFILE = "start_profile"
# A hub capture older than this is no longer what the room "was".
CAPTURE_MAX_AGE_SECONDS = 24 * 3600
# Devices that went off this long before the room went dark were on with the
# rest of it.
OFF_SEQUENCE_SECONDS = 120
# Why there is nothing to turn on.
REASON_NOTHING_SAVED = "nothing_saved"
REASON_UNAVAILABLE = "unavailable"

_UNAVAILABLE = frozenset({"unknown", "unavailable"})
_DOC_VERSION = 1
# A saved set larger than this is not one of ours.
_MAX_SET = 256


def is_on_domain(entity_id: str) -> bool:
    """True for a device one tap may turn on: a light or a fan."""
    return entity_id.split(".", 1)[0] in ON_DOMAINS


def may_come_on(entity_id: str, kinds: Mapping[str, str]) -> bool:
    """True for a light or fan, or a switch kinds names (a wall relay the
    installer typed as a light or fan: now_data.lighting_kind)."""
    domain = entity_id.split(".", 1)[0]
    return domain in ON_DOMAINS or (domain == RELAY_DOMAIN and entity_id in kinds)


# -- the plan -------------------------------------------------------------------


class RoomOnPlan(NamedTuple):
    """What one tap "on" does in a room."""

    # capture, last_on or start_profile; None: nothing to turn on.
    source: str | None
    # The source's devices that are in the room and available (on or off),
    # in the source's order: the ones that are off get turn_on.
    targets: list[str]
    # The source's devices that are in the room but unknown or unavailable:
    # skipped and reported.
    unavailable: list[str]
    # With no source: why (REASON_NOTHING_SAVED or REASON_UNAVAILABLE).
    reason: str | None = None
    # The capture's ACs that are available (on or off): the ones that are off
    # get their captured mode and target. Offline ones are in unavailable.
    acs: tuple[str, ...] = ()


def capture_is_fresh(captured_at: Any, now_t: float) -> bool:
    """True for a capture made at most CAPTURE_MAX_AGE_SECONDS ago.

    captured_at is the ISO 8601 time now_data saved; a missing or unreadable
    one is not fresh.
    """
    if not isinstance(captured_at, str):
        return False
    try:
        at = datetime.fromisoformat(captured_at)
    except ValueError:
        return False
    if at.tzinfo is None:
        return False
    return now_t - at.timestamp() <= CAPTURE_MAX_AGE_SECONDS


def capture_counts(captured_at: Any, last_on: LastOn | None, now_t: float) -> bool:
    """True when the hub's capture is still what the room was last time.

    It must be fresh (capture_is_fresh), and the room must not have gone
    dark again since: a last-on set newer than the capture by more than
    OFF_SEQUENCE_SECONDS means someone lit the room after the hub's off and
    switched it off another way. (The hub's own off makes both: the set at
    the moment the room went dark, the capture just after.)
    """
    if not capture_is_fresh(captured_at, now_t):
        return False
    if last_on is None:
        return True
    captured_t = datetime.fromisoformat(captured_at).timestamp()
    return last_on.at <= captured_t + OFF_SEQUENCE_SECONDS


def plan_room(
    *,
    capture: tuple[list[str], str | None],
    last_on: LastOn | None,
    start_profile: Iterable[str],
    devices: Mapping[str, str],
    now_t: float,
    kinds: Mapping[str, str] | None = None,
    capture_acs: Mapping[str, str] | None = None,
) -> RoomOnPlan:
    """plan_room_on over a room's three sources.

    capture is the room's restore set and when it was made
    (NowDataEngine.restore_capture); one that doesn't count
    (capture_counts) is passed empty. capture_acs are the capture's ACs that
    can still come back (now_data.ac_restore_steps), by entity id, with their
    state; they go with the capture, so an AC never comes back without one.
    An available one makes the capture the source even with no light in it:
    an off that found only the AC running brings back only the AC.
    """
    capture_ids, captured_at = capture
    counts = capture_counts(captured_at, last_on, now_t)
    capture_lights = capture_ids if counts else []
    acs = capture_acs if counts and capture_acs else {}
    available = tuple(eid for eid, state in acs.items() if state not in _UNAVAILABLE)
    offline = [eid for eid, state in acs.items() if state in _UNAVAILABLE]
    if available:
        present = present_ids(capture_lights, devices, kinds)
        return RoomOnPlan(
            SOURCE_CAPTURE,
            [eid for eid in present if devices[eid] not in _UNAVAILABLE],
            [*(eid for eid in present if devices[eid] in _UNAVAILABLE), *offline],
            acs=available,
        )
    plan = plan_room_on(
        [
            (SOURCE_CAPTURE, capture_lights),
            (SOURCE_LAST_ON, last_on.entity_ids if last_on is not None else []),
            (SOURCE_START_PROFILE, start_profile),
        ],
        devices,
        kinds,
    )
    if not offline:
        return plan
    if plan.source is None:
        return plan._replace(reason=REASON_UNAVAILABLE)
    if plan.source == SOURCE_CAPTURE:
        return plan._replace(unavailable=[*plan.unavailable, *offline])
    return plan


def present_ids(
    ids: Iterable[str],
    devices: Mapping[str, str],
    kinds: Mapping[str, str] | None = None,
) -> list[str]:
    """The ids that are still devices one tap may switch in the room."""
    kinds = kinds or {}
    return [
        eid for eid in dict.fromkeys(ids) if eid in devices and may_come_on(eid, kinds)
    ]


def plan_room_on(
    sources: Iterable[tuple[str, Iterable[str]]],
    devices: Mapping[str, str],
    kinds: Mapping[str, str] | None = None,
) -> RoomOnPlan:
    """Pick the lights and fans one tap "on" turns on.

    sources are (source, entity ids) in priority order (a stale capture is
    passed empty). devices are the room's devices one tap may switch, by
    entity id, with their state: policy-approved, served, in the room, and a
    light or fan (now_data.lighting_kind). kinds maps each of them to
    "light" or "fan"; a switch comes on only when it names it (a typed wall
    relay), so a plug passed by mistake never does. Anything else in a
    source is left out: deleted, moved, unapproved, not a light or fan. The
    first source with an available device wins.
    """
    any_unavailable = False
    for source, ids in sources:
        present = present_ids(ids, devices, kinds)
        targets = [eid for eid in present if devices[eid] not in _UNAVAILABLE]
        unavailable = [eid for eid in present if devices[eid] in _UNAVAILABLE]
        if targets:
            return RoomOnPlan(source, targets, unavailable)
        any_unavailable = any_unavailable or bool(unavailable)
    return RoomOnPlan(
        None,
        [],
        [],
        REASON_UNAVAILABLE if any_unavailable else REASON_NOTHING_SAVED,
    )


def one_tap_summary(
    plan: RoomOnPlan, kinds: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The Now page's rank.one_tap for a room that takes part in room commands.

    kinds maps a target to "light" or "fan" (now_data.lighting_kind), so a
    wall relay typed as a light counts as a light; a target without one
    counts by its domain. ac_count is there only when the capture brings an
    AC back.
    """
    if plan.source is None:
        return {"action": "open_room", "reason": plan.reason}
    kinds = kinds or {}

    def kind(entity_id: str) -> str:
        return kinds.get(entity_id) or entity_id.split(".", 1)[0]

    summary = {
        "action": "turn_on",
        "source": plan.source,
        "light_count": sum(kind(eid) == "light" for eid in plan.targets),
        "fan_count": sum(kind(eid) == "fan" for eid in plan.targets),
        "unavailable_count": len(plan.unavailable),
    }
    if plan.acs:
        summary["ac_count"] = len(plan.acs)
    return summary


# -- the last-on sets -----------------------------------------------------------


class LastOn(NamedTuple):
    """A room's last-on set."""

    entity_ids: tuple[str, ...]
    # When the room went dark (epoch seconds).
    at: int


class LastOnSets:
    """Every room's last-on set, and the lit devices that decide it.

    The sets are the saved state (load, drain); which devices are lit is
    rebuilt from the live states each time the recorder becomes ready
    (seed), then followed change by change (update).
    """

    def __init__(self) -> None:
        self._sets: dict[str, LastOn] = {}
        # room -> its lit devices; lit device -> its room.
        self._lit: dict[str, set[str]] = {}
        self._room_of: dict[str, str] = {}
        # room -> {device: (when it went off, order)} while the room stayed
        # lit; the order sorts offs within one second.
        self._fading: dict[str, dict[str, tuple[int, int]]] = {}
        self._offs = 0
        # Rooms whose set changed (or went) since the last drain.
        self._dirty: set[str] = set()

    # -- reads --------------------------------------------------------------

    def get(self, room_id: str) -> LastOn | None:
        """The room's last-on set, None when it has none."""
        return self._sets.get(room_id)

    def entity_ids(self, room_id: str) -> list[str]:
        """The room's last-on devices, newest off first (none: [])."""
        last = self._sets.get(room_id)
        return list(last.entity_ids) if last is not None else []

    def __len__(self) -> int:
        return len(self._sets)

    # -- the saved state ----------------------------------------------------

    def load(self, items: Iterable[tuple[str, Any]]) -> None:
        """Take the saved sets ((room id, doc) pairs); unreadable ones are skipped."""
        for room_id, doc in items:
            last = _from_doc(doc)
            if isinstance(room_id, str) and last is not None:
                self._sets[room_id] = last

    def drain(self) -> dict[str, dict[str, Any] | None]:
        """The changes to save: room id -> its doc, None to delete it."""
        changes = {
            room_id: _to_doc(self._sets[room_id]) if room_id in self._sets else None
            for room_id in sorted(self._dirty)
        }
        self._dirty.clear()
        return changes

    def mark_unsaved(self, room_ids: Iterable[str]) -> None:
        """Save these rooms again next time (their write failed)."""
        self._dirty.update(room_ids)

    @property
    def unsaved(self) -> bool:
        return bool(self._dirty)

    def forget_rooms_except(self, room_ids: Iterable[str]) -> None:
        """Drop the sets of rooms that no longer exist."""
        keep = set(room_ids)
        for room_id in [r for r in self._sets if r not in keep]:
            del self._sets[room_id]
            self._dirty.add(room_id)
        for room_id in [r for r in self._lit if r not in keep]:
            for entity_id in self._lit.pop(room_id):
                self._room_of.pop(entity_id, None)
        for room_id in [r for r in self._fading if r not in keep]:
            del self._fading[room_id]

    def clear(self) -> None:
        """Forget everything, saved or not (factory reset)."""
        self._sets.clear()
        self._lit.clear()
        self._room_of.clear()
        self._fading.clear()
        self._dirty.clear()

    # -- following the devices ----------------------------------------------

    def seed(self, lit: Iterable[tuple[str, str]]) -> None:
        """Start following from the devices that are on now: (entity id, room id).

        Writes no set: whatever happened before is unknown. A room that is
        still lit keeps its fading devices that are still off, so a registry
        edit while a wall plate is switched off gang by gang (the recorder
        reseeds on each) doesn't drop the gangs already off.
        """
        fading = self._fading
        self._lit.clear()
        self._room_of.clear()
        self._fading = {}
        for entity_id, room_id in lit:
            self._lit.setdefault(room_id, set()).add(entity_id)
            self._room_of[entity_id] = room_id
        for room_id, offs in fading.items():
            kept = {e: off for e, off in offs.items() if e not in self._room_of}
            if room_id in self._lit and kept:
                self._fading[room_id] = kept

    def update(
        self,
        entity_id: str,
        room_id: str | None,
        lit: bool,
        *,
        turned_off: bool,
        t: int,
    ) -> bool:
        """Take one state change of a tracked device; True when a set changed.

        room_id is the device's room now (None: none, or no longer tracked);
        lit says it is on now; turned_off says this change was an on -> off.
        """
        old_room = self._room_of.get(entity_id)
        new_room = room_id if lit else None
        if old_room == new_room:
            # Still lit in the same room (a brightness change), or still not lit.
            return False
        changed = False
        if old_room is not None:
            del self._room_of[entity_id]
            lit_now = self._lit[old_room]
            lit_now.discard(entity_id)
            went_off = turned_off and room_id == old_room
            if not lit_now:
                del self._lit[old_room]
                fading = self._fading.pop(old_room, {})
                if went_off:
                    self._sets[old_room] = LastOn(
                        (entity_id, *_newest_first(fading, t, entity_id)), t
                    )
                    self._dirty.add(old_room)
                    changed = True
            elif went_off:
                self._offs += 1
                fading = self._fading.setdefault(old_room, {})
                fading[entity_id] = (t, self._offs)
                for other in [
                    e for e, (at, _) in fading.items() if t - at > OFF_SEQUENCE_SECONDS
                ]:
                    del fading[other]
        if new_room is not None:
            # A room's fading devices exist only while it is lit (they go
            # when it goes dark), so a room lighting up starts with none.
            self._lit.setdefault(new_room, set()).add(entity_id)
            self._room_of[entity_id] = new_room
            fading = self._fading.get(new_room)
            if fading is not None:
                # On again: not fading any more.
                fading.pop(entity_id, None)
        return changed


def _newest_first(
    fading: Mapping[str, tuple[int, int]], t: int, exclude: str
) -> list[str]:
    """The devices that went off in the OFF_SEQUENCE_SECONDS before t, the
    last one first."""
    recent = [
        (off, entity_id)
        for entity_id, off in fading.items()
        if entity_id != exclude and t - off[0] <= OFF_SEQUENCE_SECONDS
    ]
    return [entity_id for _, entity_id in sorted(recent, reverse=True)]


def _to_doc(last: LastOn) -> dict[str, Any]:
    return {"v": _DOC_VERSION, "entity_ids": list(last.entity_ids), "at": last.at}


def _from_doc(doc: Any) -> LastOn | None:
    if not isinstance(doc, dict) or doc.get("v") != _DOC_VERSION:
        return None
    ids, at = doc.get("entity_ids"), doc.get("at")
    if (
        not isinstance(ids, list)
        or not 0 < len(ids) <= _MAX_SET
        or not all(isinstance(eid, str) and "." in eid for eid in ids)
        or isinstance(at, bool)
        or not isinstance(at, int)
        or at < 0
    ):
        return None
    return LastOn(tuple(dict.fromkeys(ids)), at)
