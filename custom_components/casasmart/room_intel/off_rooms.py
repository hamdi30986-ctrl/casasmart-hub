"""The all-off ranking's rules: what an off room still has going on.

ranking orders the rooms with nothing on (by its own on rule) in tiers, the
first one a room qualifies for wins:

1. running_load: something still runs while nothing counted is on: a media
   player playing, or one of the room's own meters drawing at least
   RUNNING_LOAD_MIN_W (and, once it has, until it drops below
   RUNNING_LOAD_CLEAR_W: LoadLatch). Higher watts first.
2. just_off: the room went dark less than JUST_OFF_WINDOW_SECONDS ago, by an
   off someone made (not an automation, not a hub scene). Newest first. Taken
   from the room activity log.
3. The learned tier, in its sub-slots: habit_now (room_intel.habits, through
   ranking.order_off_rooms' learned argument), then the rooms the viewer
   switched off themselves from the app in the same window (just_off with
   by_you: their own tap is demoted, so the room they just left doesn't lead
   their page, and never reads "usually on around now" either), then most
   used (not built).
4. room_order: sort_order, then name, then room id.

Nothing here imports Home Assistant, like activity and meters.
"""

from __future__ import annotations

import math
from collections.abc import Container, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from .activity import (
    ACTION_OFF,
    ACTION_ON,
    ACTION_SET,
    SOURCE_APP,
    SOURCE_AUTOMATION,
    SOURCE_SCENE,
)

REASON_ACTIVE = "active"
REASON_RUNNING_LOAD = "running_load"
REASON_JUST_OFF = "just_off"
REASON_ROOM_ORDER = "room_order"
REASON_HABIT_NOW = "habit_now"

# The tiers' sort positions. A learned tier sorts in its own slot
# between just_off and room_order.
TIER_RUNNING_LOAD = 0
TIER_JUST_OFF = 1
TIER_LEARNED = 2
TIER_ROOM_ORDER = 3
# Within the learned slot, the first item of a room's sort key: a habit for
# this hour, then a room the viewer switched off themselves, then most
# used (not built yet).
LEARNED_HABIT = 0
LEARNED_OWN_OFF = 1
LEARNED_MOST_USED = 2

LOAD_POWER = "power"
LOAD_MEDIA = "media"

# One meter at this many watts is a load, not standby: networked standby is
# a few watts and a router under 10 W, while a fan, a TV or a heater is well
# above it.
RUNNING_LOAD_MIN_W = 20.0
# Once it has, the meter keeps the room running until it reads below this, so
# a device idling around 20 W (a fridge, an AV rack) doesn't flip in and out
# of running_load from one refresh to the next.
RUNNING_LOAD_CLEAR_W = 14.0
# How long a room stays "just off" (30 minutes).
JUST_OFF_WINDOW_SECONDS = 1800
# After Home Assistant starts, devices still report the state they reached
# while it was down; an off logged then has no real time.
STARTUP_QUIET_SECONDS = 600
# An event further ahead than this comes from a clock that went backwards.
_CLOCK_SKEW_SECONDS = 60

# The log actions that tell whether a counted device was on or off.
DARK_ACTIONS = (ACTION_ON, ACTION_OFF, ACTION_SET)
# Offs that say nothing about a person leaving the room.
_UNCOUNTED_OFF_SOURCES = frozenset({SOURCE_AUTOMATION, SOURCE_SCENE})


class RecentActivity(NamedTuple):
    """The room activity the just_off tier ranks from."""

    # The rooms' on/off/set events of the last JUST_OFF_WINDOW_SECONDS,
    # newest first: {t, room_id, entity_id, action, source, member_id}.
    events: list[Mapping[str, Any]]
    now_t: float
    # The member looking (their own app taps don't count), None for none.
    member_id: str | None = None
    # When Home Assistant started (None after a reload): offs logged in the
    # STARTUP_QUIET_SECONDS after it don't count.
    started_at: float | None = None


class OffRoomFacts(NamedTuple):
    """What an off room's tier is decided from."""

    loads: list[str]
    watts: float | None
    # When it went dark by someone else's off (or no one's in particular)...
    went_dark_t: int | None
    # ...or by the viewer's own app tap.
    own_off_t: int | None = None


class WentDark(NamedTuple):
    """When an off room went dark, and whether the viewer did it from the app."""

    t: int
    by_viewer: bool


def meter_runs(watts: float, held: bool) -> bool:
    """True for a meter reading that is a running load.

    held says the meter read RUNNING_LOAD_MIN_W or more and hasn't read below
    RUNNING_LOAD_CLEAR_W since (LoadLatch).
    """
    return watts >= RUNNING_LOAD_MIN_W or (held and watts >= RUNNING_LOAD_CLEAR_W)


class LoadLatch:
    """The power meters holding a running load (meter_runs' held).

    Fed each reading as it comes (the recorder, from Home Assistant's state
    changes), so whether a meter holds depends on its readings, not on when
    someone looked. In memory only: it starts empty.
    """

    def __init__(self) -> None:
        self._held: set[str] = set()

    def update(self, entity_id: str, watts: float | None) -> None:
        """Take a meter's reading; None (no valid reading) lets go."""
        if watts is not None and meter_runs(watts, entity_id in self._held):
            self._held.add(entity_id)
        else:
            self._held.discard(entity_id)

    def held(self) -> frozenset[str]:
        """The meters holding now."""
        return frozenset(self._held)

    def forget_except(self, entity_ids: Iterable[str]) -> None:
        """Let go of every meter not in entity_ids."""
        self._held.intersection_update(entity_ids)


def still_running(
    media_playing: int,
    meter_watts: Mapping[str, float],
    held: Container[str] = frozenset(),
) -> list[str]:
    """The loads still running in a room with nothing counted on.

    meter_watts are the room's valid meter readings by entity id; held are
    the meters holding a running load (LoadLatch.held).
    """
    loads = []
    if any(meter_runs(watts, eid in held) for eid, watts in meter_watts.items()):
        loads.append(LOAD_POWER)
    if media_playing:
        loads.append(LOAD_MEDIA)
    return loads


def is_playing(state: Any) -> bool:
    """True for a media player that is playing and not excluded."""
    return (
        state.entity_id.startswith("media_player.")
        and state.state == "playing"
        and state.attributes.get("casasmart_room_activity_exclude") is not True
    )


def changed_at(state: Any) -> float | None:
    """When the state last changed (epoch seconds), None when unknown."""
    stamp = getattr(state, "last_changed_timestamp", None)
    if isinstance(stamp, int | float) and not isinstance(stamp, bool):
        return float(stamp)
    changed = getattr(state, "last_changed", None)
    if isinstance(changed, datetime):
        return changed.timestamp()
    return None


def events_by_room(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Group events (newest first) by the room they happened in."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        grouped.setdefault(event["room_id"], []).append(event)
    return grouped


def went_dark(
    events: Iterable[Mapping[str, Any]],
    counted_ids: set[str],
    unavailable_since: Iterable[float | None],
    activity: RecentActivity,
) -> WentDark | None:
    """When an off room went dark by an off that counts, or None.

    events are the room's, newest first. counted_ids are the devices that
    count it as on today (unavailable ones included); unavailable_since is
    when each of those that is unknown or unavailable got so. The newest
    on/off/set of a counted device must be an off inside the window: an on
    means the device went dark unlogged (through unavailable, or while Home
    Assistant was down). An off logged in the first minutes after Home
    Assistant started is a device catching up with what happened while it
    was down, at no known time. A counted device that went unavailable at or
    after the off may be what made the room look dark, so it blocks too.
    by_viewer says the off was the viewing member's own app tap.
    """
    newest = next(
        (
            event
            for event in events
            if event["entity_id"] in counted_ids and event["action"] in DARK_ACTIONS
        ),
        None,
    )
    if newest is None or newest["action"] != ACTION_OFF:
        return None
    t = newest["t"]
    now_t = activity.now_t
    if not now_t - JUST_OFF_WINDOW_SECONDS <= t <= now_t + _CLOCK_SKEW_SECONDS:
        return None
    started_at = activity.started_at
    # Event times are whole seconds: compare from the second it started in.
    if (
        started_at is not None
        and math.floor(started_at) <= t < started_at + STARTUP_QUIET_SECONDS
    ):
        return None
    source = newest["source"]
    if source in _UNCOUNTED_OFF_SOURCES:
        return None
    if any(since is None or since >= t for since in unavailable_since):
        return None
    return WentDark(
        int(t),
        activity.member_id is not None
        and source == SOURCE_APP
        and newest.get("member_id") == activity.member_id,
    )


def iso_utc(t: float) -> str:
    """An epoch time as ISO 8601 in UTC."""
    return datetime.fromtimestamp(t, UTC).isoformat()
