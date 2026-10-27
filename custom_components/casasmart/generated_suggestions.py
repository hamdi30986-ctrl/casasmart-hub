"""Generated room scenes: built here, never run or saved here.

Pure functions behind GeneratedSuggestionRuntime. From a room's current
device states they build the actions for "turn the room off", "save energy
in the room" or "turn on the room you usually use now" and wrap them as a
suggestion for the current two-hour window. A client gets the kinds it asks
for (?kinds=, ask_from_query); without asking, the first two only. A run
goes through async_execute_registry_scene, which re-checks each action
against live state. The contract is in docs/api/GENERATED_ROOM_SUGGESTIONS_V1.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .room_intel.habits import LAYER_HOUSEHOLD, LAYER_PERSONAL, LAYERS
from .suggestions import SuggestionError

KIND_ROOM_OFF = "room_off"
KIND_ROOM_ECO = "room_eco"
# A room that is off but usually on at this hour (room_intel.habits), with
# the devices that usually come on there.
KIND_USUALLY_ON_NOW = "usually_on_now"
KINDS = frozenset({KIND_ROOM_OFF, KIND_ROOM_ECO, KIND_USUALLY_ON_NOW})
# What a client that sends no ?kinds= gets: the kinds every app shows.
DEFAULT_KINDS = frozenset({KIND_ROOM_OFF, KIND_ROOM_ECO})
_MAX_KINDS_LENGTH = 256
# The most usually_on_now plans offered at once (one per room).
USUAL_ROOMS = 2

_WINDOW_SECONDS = 2 * 60 * 60
# The eco plan's light cap (of 255) and its AC target in each unit.
_ECO_BRIGHTNESS = 128
_ECO_TARGETS = {"°C": 24, "C": 24, "°F": 75.2, "F": 75.2}
# Home Assistant's ClimateEntityFeature bits.
_SUPPORT_TARGET_TEMPERATURE = 1
_SUPPORT_FAN_MODE = 8


@dataclass(frozen=True)
class Ask:
    """What a generated suggestions request asks for: the kinds the client
    shows, and whether the habits are the household's (the shared tablet)
    or the caller's own blended over them."""

    kinds: frozenset[str] = DEFAULT_KINDS
    household: bool = False


def ask_from_query(query: Mapping[str, str]) -> Ask:
    """The Ask in a request's query: ?kinds= and ?habits=.

    kinds is a comma-separated list; names this hub doesn't know are
    skipped, so a newer app still gets the kinds it shares with an older
    hub. habits is household or personal (the default). A bad value is a
    SuggestionError (400).
    """
    kinds = DEFAULT_KINDS
    raw = query.get("kinds")
    if raw is not None:
        if len(raw) > _MAX_KINDS_LENGTH:
            raise SuggestionError("invalid_kinds")
        names = {name.strip() for name in raw.split(",")} - {""}
        if not names:
            raise SuggestionError("invalid_kinds")
        kinds = frozenset(names & KINDS)
    layer = query.get("habits", LAYER_PERSONAL)
    if layer not in LAYERS:
        raise SuggestionError("invalid_habits")
    return Ask(kinds, layer == LAYER_HOUSEHOLD)


def digest(value):
    """A stable SHA-256 of a JSON-serializable value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def window(now):
    """The two-hour UTC window holding now: (start epoch, end datetime)."""
    start = int(now.timestamp()) // _WINDOW_SECONDS * _WINDOW_SECONDS
    return start, datetime.fromtimestamp(start + _WINDOW_SECONDS, UTC)


def number(value):
    """True for a finite int or float (not a bool) that fits in a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def room_actions(states, kind, *, temperature_unit="°C"):
    """The actions of a room_off or room_eco plan for these states.

    Only lights and cooling-capable ACs are touched, with absolute commands;
    a device's name is never used to guess what it is.
    """
    states = [
        s
        for s in states
        if s.attributes.get("casasmart_room_activity_exclude") is not True
    ]
    lights = sorted(
        (s for s in states if s.entity_id.startswith("light.") and s.state == "on"),
        key=lambda s: s.entity_id,
    )
    # Cooling capability identifies an AC; a heating-only thermostat is not one.
    acs = sorted(
        (
            s
            for s in states
            if s.entity_id.startswith("climate.")
            and s.state in {"cool", "heat", "auto", "heat_cool", "dry", "fan_only"}
            and "cool" in s.attributes.get("hvac_modes", [])
        ),
        key=lambda s: s.entity_id,
    )
    actions = []

    def add(state, action, data=None):
        actions.append(
            {
                "entity_id": state.entity_id,
                "action": action,
                "data": data or {},
                "name": state.attributes.get("friendly_name") or state.entity_id,
            }
        )

    if kind == "room_off":
        for state in lights + acs:
            if state in acs and "off" not in state.attributes.get("hvac_modes", []):
                continue
            add(state, "turn_off")
        return actions

    # Leave at least one light on. Dimmable lights are capped at 50% and never
    # brightened; non-dimmable ones are switched off in entity id order.
    nondimmable = []
    for state in lights:
        attrs = state.attributes
        modes = set(attrs.get("supported_color_modes", [])) - {"onoff", "unknown"}
        if modes and number(attrs.get("brightness")):
            if attrs["brightness"] > _ECO_BRIGHTNESS:
                add(state, "turn_on", {"brightness": _ECO_BRIGHTNESS})
        else:
            nondimmable.append(state)
    for state in nondimmable[: min(len(lights) // 2, max(0, len(lights) - 1))]:
        add(state, "turn_off")
    for state in acs:
        attrs = state.attributes
        if state.state != "cool":
            continue  # never change the mode, or a heat or auto target
        unit = attrs.get("temperature_unit", temperature_unit)
        target = _ECO_TARGETS.get(unit)
        current = attrs.get("temperature")
        minimum, maximum = attrs.get("min_temp"), attrs.get("max_temp")
        step = attrs.get("target_temp_step", 1 if unit in {"°C", "C"} else 0.1)
        supported_target = target is not None and number(step) and step > 0
        if supported_target:
            origin = minimum if number(minimum) else 0
            supported_target = (
                abs((target - origin) / step - round((target - origin) / step)) < 0.001
            )
        if (
            supported_target
            and number(current)
            and current < target
            and (not number(minimum) or minimum <= target)
            and (not number(maximum) or maximum >= target)
            and int(attrs.get("supported_features", 0)) & _SUPPORT_TARGET_TEMPERATURE
        ):
            add(state, "set_temperature", {"temperature": target})
        if (
            # fan_modes is null while a fan-capable AC has reported none.
            "low" in (attrs.get("fan_modes") or [])
            and attrs.get("fan_mode") != "low"
            and int(attrs.get("supported_features", 0)) & _SUPPORT_FAN_MODE
        ):
            add(state, "set_fan_mode", {"fan_mode": "low"})
    return actions


def usual_step_applies(state, step):
    """True while a usually_on_now step still applies: it turns on a light,
    fan or wall relay that is off and not left out of room activity."""
    return (
        step["action"] == "turn_on"
        and state.entity_id.split(".", 1)[0] in {"light", "fan", "switch"}
        and state.state == "off"
        and state.attributes.get("casasmart_room_activity_exclude") is not True
    )


def usual_actions(states, levels: Mapping[str, Mapping[str, Any]] | None = None):
    """The actions of a usually_on_now plan: turn each of these devices on
    that is off, in this order.

    states are the room's lights and fans that usually come on around now
    (the caller picks them: now_data.room_light_kind, HabitQuery.
    usual_devices). levels, when given, maps a device to its turn_on data
    (a brightness, a fan speed); nothing passes them yet, so every device
    comes on plainly, at the level it keeps.
    """
    levels = levels or {}
    actions = []
    for state in states:
        step = {
            "entity_id": state.entity_id,
            "action": "turn_on",
            "data": dict(levels.get(state.entity_id, {})),
            "name": state.attributes.get("friendly_name") or state.entity_id,
        }
        if usual_step_applies(state, step):
            actions.append(step)
    return actions


def _plan(room, kind, actions, now, *, name, icon, reason):
    """The suggestion around a plan's actions.

    suppression_id names the room, kind and window, so a dismissal survives
    the plan changing; occurrence_id also covers the actions, so running a
    plan that changed since it was shown is refused.
    """
    start, expires = window(now)
    slot = digest(["generated_room_v1", start, room["room_id"], kind])
    occurrence = digest([slot, actions])
    return {
        "source": "generated_room_v1",
        "kind": kind,
        "room_id": room["room_id"],
        "room_name": room["name"],
        "rule_id": slot,
        "suppression_id": slot,
        "occurrence_id": occurrence,
        "scene_id": occurrence,
        "scene": {"scene_id": occurrence, "name": name, "icon": icon},
        "actions": actions,
        "generated_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "reason": reason,
    }


def make_suggestion(room, kind, states, now, *, temperature_unit="°C"):
    """A room_off or room_eco suggestion for the room, or None when there is
    nothing to do."""
    actions = room_actions(states, kind, temperature_unit=temperature_unit)
    if not actions:
        return None
    return _plan(
        room,
        kind,
        actions,
        now,
        name=("Turn off · " if kind == KIND_ROOM_OFF else "Save energy · ")
        + room["name"],
        icon="leaf" if kind == KIND_ROOM_ECO else "power",
        reason={"code": "active_room", "active_count": room["active_count"]},
    )


def make_usual_suggestion(room, states, now, *, layer, hour, levels=None):
    """A usually_on_now suggestion for the room, or None when none of its
    usual devices can be turned on.

    states are its usual devices (usual_actions); layer is the habit layer
    that answered (household or personal) and hour the local hour it is
    usual at.
    """
    actions = usual_actions(states, levels)
    if not actions:
        return None
    return _plan(
        room,
        KIND_USUALLY_ON_NOW,
        actions,
        now,
        name="Turn on · " + room["name"],
        icon="lightbulb",
        reason={"code": "usually_on_now", "layer": layer, "hour": hour},
    )
