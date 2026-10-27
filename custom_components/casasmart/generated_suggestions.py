"""Generated room scenes: built here, never run or saved here.

Pure functions behind GeneratedSuggestionRuntime. From a room's current
device states they build the actions for "turn the room off", "save energy
in the room", "turn on the room you usually use now" (at the levels last
used at this hour) or "turn off what you usually have off by now" and wrap
them as a suggestion for the current two-hour window. A client gets the kinds it asks for (?kinds=, ask_from_query);
without asking, the first two only. A run goes through
async_execute_registry_scene, which re-checks each action against live
state. The contract is in docs/api/GENERATED_ROOM_SUGGESTIONS_V1.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .now_data import AC_ON_MODES, is_room_ac
from .room_intel.habits import LAYER_HOUSEHOLD, LAYER_PERSONAL, LAYERS
from .suggestions import SuggestionError

KIND_ROOM_OFF = "room_off"
KIND_ROOM_ECO = "room_eco"
# A room that is off but usually on at this hour (room_intel.habits), with
# the devices that usually come on there.
KIND_USUALLY_ON_NOW = "usually_on_now"
# A room's devices that are on but usually switched off by now
# (room_intel.habits).
KIND_USUALLY_OFF_NOW = "usually_off_now"
KINDS = frozenset(
    {KIND_ROOM_OFF, KIND_ROOM_ECO, KIND_USUALLY_ON_NOW, KIND_USUALLY_OFF_NOW}
)
# What a client that sends no ?kinds= gets: the kinds every app shows.
DEFAULT_KINDS = frozenset({KIND_ROOM_OFF, KIND_ROOM_ECO})
_MAX_KINDS_LENGTH = 256
# The most usually_on_now plans offered at once (one per room), and the
# most usually_off_now plans.
USUAL_ROOMS = 2

_WINDOW_SECONDS = 2 * 60 * 60
# The eco plan's light cap (of 255) and its AC target in each unit.
_ECO_BRIGHTNESS = 128
_ECO_TARGETS = {"°C": 24, "C": 24, "°F": 75.2, "F": 75.2}
# Home Assistant's ClimateEntityFeature and FanEntityFeature bits.
_SUPPORT_TARGET_TEMPERATURE = 1
_SUPPORT_FAN_MODE = 8
_SUPPORT_SET_SPEED = 1
# What a usually_on_now step may send: a turn_on with these level keys, or
# one of an AC's three steps with its one key.
_USUAL_ON_DATA = {
    "light": frozenset({"brightness", "color_temp_kelvin"}),
    "fan": frozenset({"percentage"}),
    "switch": frozenset(),
}
_USUAL_AC_STEPS = {
    "set_hvac_mode": "hvac_mode",
    "set_temperature": "temperature",
    "set_fan_mode": "fan_mode",
}


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


def usual_step_applies(state, step, steps=()):
    """True while a usually_on_now step still applies to the device.

    A turn_on of a light, fan or wall relay that is off, with only the level
    data that device takes; or one of an AC's steps (mode, target, fan) with
    its one value: the mode while the AC is off, the target and fan while it
    is off or in the mode the plan's own mode step sets (steps are the plan's
    actions). Never a device left out of room activity.
    """
    if state.attributes.get("casasmart_room_activity_exclude") is True:
        return False
    domain = state.entity_id.split(".", 1)[0]
    data = step.get("data") or {}
    if domain in _USUAL_ON_DATA:
        return (
            step["action"] == "turn_on"
            and state.state == "off"
            and set(data) <= _USUAL_ON_DATA[domain]
        )
    key = _USUAL_AC_STEPS.get(step["action"])
    if domain != "climate" or key is None or set(data) != {key}:
        return False
    if not is_room_ac(state):
        return False
    if step["action"] == "set_hvac_mode":
        return state.state == "off"
    mode = next(
        (
            other["data"]["hvac_mode"]
            for other in steps
            if other["entity_id"] == state.entity_id
            and other["action"] == "set_hvac_mode"
        ),
        None,
    )
    return mode is not None and state.state in {"off", mode}


def usual_off_step_applies(state, step):
    """True while a usually_off_now step still applies to the device.

    A plain turn_off (no data) of a light, fan or wall relay that is on, or
    of an AC running in a mode (is_room_ac) that has off among its modes,
    as room_off's ACs. Never a device left out of room activity.
    """
    attributes = state.attributes
    if (
        attributes.get("casasmart_room_activity_exclude") is True
        or step["action"] != "turn_off"
        or step.get("data")
    ):
        return False
    if state.entity_id.split(".", 1)[0] in _USUAL_ON_DATA:
        return state.state == "on"
    return (
        is_room_ac(state)
        and state.state in AC_ON_MODES
        and "off" in (attributes.get("hvac_modes") or [])
    )


def usual_off_actions(states):
    """The actions of a usually_off_now plan, device by device in this order:
    a turn_off of each that is still on (usual_off_step_applies).

    states are the room's devices that are on and usually switched off by
    now (the caller picks them: now_data.room_on_kind, so never a plug, an
    outlet, a heater, a lock, a cover or a TV; HabitQuery.usual_off_devices).
    """
    actions = []
    for state in states:
        step = {
            "entity_id": state.entity_id,
            "action": "turn_off",
            "data": {},
            "name": state.attributes.get("friendly_name") or state.entity_id,
        }
        if usual_off_step_applies(state, step):
            actions.append(step)
    return actions


def _turn_on_data(state, level):
    """A light's or fan's turn_on data for its recorded level, keeping only
    what it supports now: a dimmable light's brightness, a colour temperature
    within its range, a fan's speed. Empty: it comes on as it keeps."""
    attrs = state.attributes
    domain = state.entity_id.split(".", 1)[0]
    data = {}
    if domain == "light":
        modes = set(attrs.get("supported_color_modes") or [])
        brightness = level.get("brightness")
        if (
            modes - {"onoff", "unknown"}
            and number(brightness)
            and 1 <= brightness <= 255
        ):
            data["brightness"] = brightness
        kelvin = level.get("color_temp_kelvin")
        low = attrs.get("min_color_temp_kelvin")
        high = attrs.get("max_color_temp_kelvin")
        if (
            "color_temp" in modes
            and number(kelvin)
            and kelvin > 0
            and (not number(low) or low <= kelvin)
            and (not number(high) or kelvin <= high)
        ):
            data["color_temp_kelvin"] = kelvin
    elif domain == "fan":
        percentage = level.get("percentage")
        if (
            int(attrs.get("supported_features", 0)) & _SUPPORT_SET_SPEED
            and number(percentage)
            and 1 <= percentage <= 100
        ):
            data["percentage"] = percentage
    return data


def _ac_steps(state, level):
    """An AC's (action, data) steps back to its recorded mode and target, and
    fan mode when it had one it still has; none without a mode and a target
    it still takes (an AC never comes on at a guess)."""
    attrs = state.attributes
    features = int(attrs.get("supported_features", 0))
    mode = level.get("hvac_mode")
    target = level.get("temperature")
    minimum, maximum = attrs.get("min_temp"), attrs.get("max_temp")
    if not (
        mode in AC_ON_MODES
        and mode in (attrs.get("hvac_modes") or [])
        and features & _SUPPORT_TARGET_TEMPERATURE
        and number(target)
        and (not number(minimum) or minimum <= target)
        and (not number(maximum) or target <= maximum)
    ):
        return []
    steps = [
        ("set_hvac_mode", {"hvac_mode": mode}),
        ("set_temperature", {"temperature": target}),
    ]
    fan_mode = level.get("fan_mode")
    if features & _SUPPORT_FAN_MODE and fan_mode in (attrs.get("fan_modes") or []):
        steps.append(("set_fan_mode", {"fan_mode": fan_mode}))
    return steps


def usual_actions(states, levels: Mapping[str, Mapping[str, Any]] | None = None):
    """The actions of a usually_on_now plan, device by device in this order:
    each one that is off comes back on at the level it was last used at
    around now.

    states are the room's lights, fans and ACs that usually come on around
    now (the caller picks them: now_data.room_light_kind, is_room_ac,
    HabitQuery.usual_devices). levels maps a device to its recorded level
    (HabitQuery.async_levels). A light or fan turns on with what of its
    level it still supports (no level: data empty, at the level it keeps); a
    wall relay plainly. An AC comes on only with a recorded mode and target:
    set_hvac_mode, set_temperature, then set_fan_mode when it had one.
    """
    levels = levels or {}
    actions = []
    for state in states:
        level = levels.get(state.entity_id) or {}
        if state.entity_id.startswith("climate."):
            pairs = _ac_steps(state, level)
        else:
            pairs = [("turn_on", _turn_on_data(state, level))]
        name = state.attributes.get("friendly_name") or state.entity_id
        steps = [
            {"entity_id": state.entity_id, "action": action, "data": data, "name": name}
            for action, data in pairs
        ]
        if steps and all(usual_step_applies(state, step, steps) for step in steps):
            actions.extend(steps)
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

    states are its usual devices and levels their recorded levels
    (usual_actions); layer is the habit layer that answered (household or
    personal) and hour the local hour it is usual at.
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


def make_usual_off_suggestion(room, states, now, *, layer, hour):
    """A usually_off_now suggestion for the room, or None when none of its
    devices can be turned off.

    states are its devices usually off by now (usual_off_actions); layer is
    the habit layer that answered (household or personal) and hour the local
    hour it is now.
    """
    actions = usual_off_actions(states)
    if not actions:
        return None
    return _plan(
        room,
        KIND_USUALLY_OFF_NOW,
        actions,
        now,
        name="Turn off · " + room["name"],
        icon="power",
        reason={"code": "usually_off_now", "layer": layer, "hour": hour},
    )
