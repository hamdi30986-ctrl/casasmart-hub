"""The room activity log's rules: what is an event, who caused it, how long it stays.

room_intel.recorder applies them to Home Assistant's state changes and writes
storage.RoomActivityTable. Nothing here imports Home Assistant.

An event is a confirmed transition of a device the hub manages (transition).
Its source follows Home Assistant's own attribution, the context on the state
change (attribute):

- app, scene, automation: a service call the hub made with its own context:
  a member's device or room command, a hub scene or suggestion a member ran,
  or Energy Saving.
- ha_user: the context carries a Home Assistant user (its UI or app, a script
  a user ran).
- automation: the context has a parent, or an automation or script started in
  it. Home Assistant runs every automation in a new context whose parent is its
  trigger's.
- unknown: a service call no one we know made (HomeKit, another integration).
- wall_switch: anything else. The device wrote its state by itself, with a
  fresh context: a wall press, a Zigbee remote, the vendor's app, a schedule
  kept on the device.

A member id is kept only for app and scene events.

Levels. An "on" or "set" carries the level the device is at (level_of): a
light's brightness and colour temperature, a fan's speed, an AC's mode,
target and fan mode. A person changing the level of a device that stays on (a
light dimmed, an AC's setpoint) is a "level" row (level_change), which is no
habit vote. A device's own report soon after it was switched on is that
switch-on's (LEVEL_FOLLOW_SECONDS: a light ramping up, an AC settling), and a
level that changes again before it is written replaces the one queued
(room_intel.recorder).
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

SOURCE_APP = "app"
SOURCE_SCENE = "scene"
SOURCE_AUTOMATION = "automation"
SOURCE_HA_USER = "ha_user"
SOURCE_WALL_SWITCH = "wall_switch"
SOURCE_UNKNOWN = "unknown"
SOURCES = frozenset(
    {
        SOURCE_APP,
        SOURCE_SCENE,
        SOURCE_AUTOMATION,
        SOURCE_HA_USER,
        SOURCE_WALL_SWITCH,
        SOURCE_UNKNOWN,
    }
)
# What the hub can tag its own service calls as.
HUB_SOURCES = frozenset({SOURCE_APP, SOURCE_SCENE, SOURCE_AUTOMATION})
# Sources whose events carry the member who acted.
MEMBER_SOURCES = frozenset({SOURCE_APP, SOURCE_SCENE})

ACTION_ON = "on"
ACTION_OFF = "off"
ACTION_SET = "set"
ACTION_OPEN = "open"
ACTION_CLOSE = "close"
# A level changed while the device stayed on: no transition, no habit vote.
ACTION_LEVEL = "level"
# The actions whose rows carry a level.
LEVEL_ACTIONS = frozenset({ACTION_ON, ACTION_SET, ACTION_LEVEL})
# The sources whose level changes are logged: the ones the habits learn from
# (room_intel.habits.SOURCE_WEIGHTS). An automation's or an unknown caller's
# level change is never a level anyone used.
LEVEL_SOURCES = frozenset(
    {SOURCE_APP, SOURCE_SCENE, SOURCE_HA_USER, SOURCE_WALL_SWITCH}
)
# How long after a switch-on a device's own level report (attribute(): a
# wall switch) is still that switch-on's.
LEVEL_FOLLOW_SECONDS = 30.0

# Retention. Human rows cover last year's Ramadan: a lunar year is about 354
# days, so when Ramadan starts again its previous one began ~354 days ago, and
# when it ends that one began ~384 days ago. Automation rows never feed a
# habit, only the latest off. The cap bounds the file (~134 bytes a row, up
# to ~30 more with a level).
HUMAN_RETENTION_DAYS = 400
AUTOMATION_RETENTION_DAYS = 31
MAX_ROWS = 500_000

_TOGGLE_DOMAINS = frozenset({"light", "fan", "switch"})
_CLIMATE_MODES = frozenset({"heat", "cool", "heat_cool", "auto", "dry", "fan_only"})
_COVER_OPEN = frozenset({"open", "opening"})
_COVER_CLOSED = frozenset({"closed", "closing"})
_MEDIA_OFF = frozenset({"off", "standby"})
_MEDIA_ON = frozenset({"on", "idle", "playing", "paused", "buffering"})


def transition(entity_id: str, old: str, new: str) -> str | None:
    """The action a state change records, or None when it isn't an event.

    Lights, fans and switches go on or off. An AC goes on (off to a mode),
    off, or is set (one mode to another). A cover opens (closed or closing to
    open or opening) or closes. A media player goes on or off (off or standby
    against any other state). Anything else, including unknown/unavailable on
    either side and a change within the same class (opening to open), is no
    event: those are flaps, restores and duplicates.
    """
    domain = entity_id.split(".", 1)[0]
    if domain in _TOGGLE_DOMAINS:
        if (old, new) == ("off", "on"):
            return ACTION_ON
        if (old, new) == ("on", "off"):
            return ACTION_OFF
        return None
    if domain == "climate":
        old_on, new_on = old in _CLIMATE_MODES, new in _CLIMATE_MODES
        if (old_on or old == "off") and (new_on or new == "off"):
            if old == "off" and new_on:
                return ACTION_ON
            if old_on and new == "off":
                return ACTION_OFF
            if old_on and new_on and old != new:
                return ACTION_SET
        return None
    if domain == "cover":
        if old in _COVER_CLOSED and new in _COVER_OPEN:
            return ACTION_OPEN
        if old in _COVER_OPEN and new in _COVER_CLOSED:
            return ACTION_CLOSE
        return None
    if domain == "media_player":
        if old in _MEDIA_OFF and new in _MEDIA_ON:
            return ACTION_ON
        if old in _MEDIA_ON and new in _MEDIA_OFF:
            return ACTION_OFF
    return None


def level_of(entity_id: str, state: str, attributes: Mapping[str, Any]) -> dict | None:
    """The level a device that is on is at, as the service data that sets it
    back; None when it is off or has none.

    A light: brightness (1-255) and, in colour temperature mode, its
    color_temp_kelvin. A fan: its percentage (1-100). An AC in a mode: that
    hvac_mode, its target temperature and fan_mode. A wall relay, a cover or
    a TV has none. A value out of range or of the wrong type is left out.
    """
    domain = entity_id.split(".", 1)[0]
    level: dict[str, Any] = {}
    if domain == "light" and state == "on":
        _put_whole(level, "brightness", attributes.get("brightness"), 255)
        _put_whole(level, "color_temp_kelvin", attributes.get("color_temp_kelvin"))
    elif domain == "fan" and state == "on":
        _put_whole(level, "percentage", attributes.get("percentage"), 100)
    elif domain == "climate" and state in _CLIMATE_MODES:
        level["hvac_mode"] = state
        target = attributes.get("temperature")
        if (
            isinstance(target, (int, float))
            and not isinstance(target, bool)
            and math.isfinite(target)
            and -1000 <= target <= 1000
        ):
            level["temperature"] = float(target)
        fan_mode = attributes.get("fan_mode")
        if isinstance(fan_mode, str) and fan_mode.strip() and len(fan_mode) <= 64:
            level["fan_mode"] = fan_mode.strip()
    return level or None


def level_change(
    entity_id: str,
    old: str,
    old_attributes: Mapping[str, Any],
    new: str,
    new_attributes: Mapping[str, Any],
) -> dict | None:
    """The new level when a device changed its level and stayed on, else None.

    On on both sides (an AC in the same mode), with a new level that differs
    from the old one. A switch-on, a switch-off and an AC's mode change are
    transitions, not level changes.
    """
    if old != new:
        return None
    level = level_of(entity_id, new, new_attributes)
    if level is None or level == level_of(entity_id, old, old_attributes):
        return None
    return level


def _put_whole(
    level: dict[str, Any], name: str, value: Any, maximum: int = 100_000
) -> None:
    """Put value in level as a whole number from 1 to maximum, if it is one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if not math.isfinite(value):
        return
    whole = round(value)
    if 1 <= whole <= maximum:
        level[name] = whole


def attribute(
    *,
    hub_origin: tuple[str, str | None] | None,
    user_id: str | None,
    parent_id: str | None,
    automation_context: bool,
    service_call_context: bool,
) -> tuple[str, str | None]:
    """The (source, member_id) of a state change from its context.

    hub_origin is what the hub tagged the context with, when it made the call;
    automation_context is whether an automation or script started in the
    context (or its parent); service_call_context is whether a service call
    was made in it. The first rule that matches wins.
    """
    if hub_origin is not None:
        source, member_id = hub_origin
        return source, member_id if source in MEMBER_SOURCES else None
    if user_id:
        return SOURCE_HA_USER, None
    if parent_id is not None or automation_context:
        return SOURCE_AUTOMATION, None
    if service_call_context:
        return SOURCE_UNKNOWN, None
    return SOURCE_WALL_SWITCH, None


class _Prunable(Protocol):
    def prune(self, *, before_t: int, sources: Iterable[str] | None = None) -> int: ...

    def trim_to(self, max_rows: int) -> int: ...


def prune(table: _Prunable, now_t: int) -> dict[str, int]:
    """Apply the retention rules to the table and say how many rows each took.

    Blocking (executor). Automation rows go first, then everything past the
    human window, then the oldest rows beyond the cap.
    """
    day = 86400
    return {
        "automation": table.prune(
            before_t=max(0, now_t - AUTOMATION_RETENTION_DAYS * day),
            sources=[SOURCE_AUTOMATION],
        ),
        "age": table.prune(before_t=max(0, now_t - HUMAN_RETENTION_DAYS * day)),
        "cap": table.trim_to(MAX_ROWS),
    }


def hour_histogram(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[int]]:
    """Fold RoomActivityTable.room_hour_counts rows into 24 bins per room."""
    histogram: dict[str, list[int]] = {}
    for row in rows:
        bins = histogram.setdefault(row["room_id"], [0] * 24)
        bins[row["local_hour"]] += row["events"]
    return histogram


class ChatterGuard:
    """Caps each device's events: a burst of `burst`, then `per_hour` an hour.

    A token bucket per entity. People don't switch one light more than that;
    a device that does is faulty or driven by a loop, and logging it all would
    push real history out through the row cap.
    """

    def __init__(self, burst: int = 20, per_hour: int = 15) -> None:
        self._burst = float(burst)
        self._rate = per_hour / 3600.0
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, entity_id: str, now: float) -> bool:
        """Spend one token for the entity at monotonic time now, if it has one."""
        tokens, at = self._buckets.get(entity_id, (self._burst, now))
        tokens = min(self._burst, tokens + (now - at) * self._rate)
        if tokens < 1.0:
            self._buckets[entity_id] = (tokens, now)
            return False
        self._buckets[entity_id] = (tokens - 1.0, now)
        return True

    def forget_except(self, entity_ids: Iterable[str]) -> None:
        """Drop the buckets of devices no longer managed."""
        keep = set(entity_ids)
        for entity_id in [e for e in self._buckets if e not in keep]:
            del self._buckets[entity_id]


class RecentIds:
    """Context ids remembered for a while: at most `size`, each for `ttl` seconds.

    Ids are unique, so the limits only bound memory; an id that ages out just
    can't be attributed any more.
    """

    def __init__(self, ttl: float, size: int) -> None:
        self._ttl = ttl
        self._size = size
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def add(self, key: str, value: Any, now: float) -> None:
        self._items[key] = (now + self._ttl, value)
        self._items.move_to_end(key)
        self._expire(now)
        while len(self._items) > self._size:
            self._items.popitem(last=False)

    def get(self, key: str | None, now: float) -> Any:
        """The value, or None when the id is unknown or has aged out."""
        if key is None:
            return None
        item = self._items.get(key)
        if item is None or item[0] <= now:
            return None
        return item[1]

    def discard_values(self, predicate) -> None:
        """Forget every id whose value matches."""
        for key in [k for k, (_, v) in self._items.items() if predicate(v)]:
            del self._items[key]

    def _expire(self, now: float) -> None:
        # Insertion order is expiry order: every id gets the same ttl.
        while self._items:
            key, (expires, _) = next(iter(self._items.items()))
            if expires > now:
                return
            del self._items[key]
