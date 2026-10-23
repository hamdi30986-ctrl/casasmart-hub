"""The all-off ranking's rules: what an off room still has going on.

ranking orders the rooms with nothing on (by its own on rule) in tiers, the
first one a room qualifies for wins:

1. running_load: something still runs while nothing counted is on: a media
   player playing, or one of the room's own meters drawing at least
   RUNNING_LOAD_MIN_W. Higher watts first.
2. just_off: the room went dark less than JUST_OFF_WINDOW_SECONDS ago, by an
   off someone made (not an automation, not a hub scene, not the viewer's
   own app tap). Newest first. Taken from the room activity log.
3. room_order: sort_order, then name, then room id.

The learned tiers (habit, most used) go between just_off and room_order,
through ranking.order_off_rooms' learned argument. Nothing here imports Home
Assistant, like activity and meters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
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
    went_dark_t: int | None


def still_running(media_playing: int, peak_watts: float | None) -> list[str]:
    """The loads still running in a room with nothing counted on."""
    loads = []
    if peak_watts is not None and peak_watts >= RUNNING_LOAD_MIN_W:
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


def went_dark_at(
    events: Iterable[Mapping[str, Any]],
    counted_ids: set[str],
    unavailable_since: Iterable[float | None],
    activity: RecentActivity,
) -> int | None:
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
    if (
        activity.member_id is not None
        and source == SOURCE_APP
        and newest.get("member_id") == activity.member_id
    ):
        return None
    if any(since is None or since >= t for since in unavailable_since):
        return None
    return int(t)


def iso_utc(t: float) -> str:
    """An epoch time as ISO 8601 in UTC."""
    return datetime.fromtimestamp(t, UTC).isoformat()
