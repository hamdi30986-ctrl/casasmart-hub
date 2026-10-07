"""Deterministic, temporary room scenes. Never dispatches or saves a scene."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def window(now):
    start = int(now.timestamp()) // 7200 * 7200
    return start, datetime.fromtimestamp(start + 7200, UTC)


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def room_actions(states, kind, *, temperature_unit="°C"):
    """Strict domain allowlist, absolute commands, no inferred appliance names."""
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

    # Leave at least one light on. Dimmable lights cap at 50%; already-dim
    # lights are never brightened. Non-dimmable selection is stable by ID.
    nondimmable = []
    for state in lights:
        attrs = state.attributes
        modes = set(attrs.get("supported_color_modes", [])) - {"onoff", "unknown"}
        if modes and number(attrs.get("brightness")):
            if attrs["brightness"] > 128:
                add(state, "turn_on", {"brightness": 128})
        else:
            nondimmable.append(state)
    for state in nondimmable[: min(len(lights) // 2, max(0, len(lights) - 1))]:
        add(state, "turn_off")
    for state in acs:
        attrs = state.attributes
        if state.state != "cool":
            continue  # Do not change HVAC mode or heat/auto targets.
        unit = attrs.get("temperature_unit", temperature_unit)
        target = 24 if unit in {"°C", "C"} else 75.2 if unit in {"°F", "F"} else None
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
            and int(attrs.get("supported_features", 0)) & 1
        ):
            add(state, "set_temperature", {"temperature": target})
        if (
            # fan_modes is null while a fan-capable AC has reported none.
            "low" in (attrs.get("fan_modes") or [])
            and attrs.get("fan_mode") != "low"
            and int(attrs.get("supported_features", 0)) & 8
        ):
            add(state, "set_fan_mode", {"fan_mode": "low"})
    return actions


def make_suggestion(room, kind, states, now, *, temperature_unit="°C"):
    actions = room_actions(states, kind, temperature_unit=temperature_unit)
    if not actions:
        return None
    start, expires = window(now)
    slot = digest(["generated_room_v1", start, room["room_id"], kind])
    occurrence = digest([slot, actions])
    name = ("Turn off · " if kind == "room_off" else "Save energy · ") + room["name"]
    return {
        "source": "generated_room_v1",
        "kind": kind,
        "room_id": room["room_id"],
        "room_name": room["name"],
        "rule_id": slot,
        "suppression_id": slot,
        "occurrence_id": occurrence,
        "scene_id": occurrence,
        "scene": {
            "scene_id": occurrence,
            "name": name,
            "icon": "leaf" if kind == "room_eco" else "power",
        },
        "actions": actions,
        "generated_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "reason": {"code": "active_room", "active_count": room["active_count"]},
    }
