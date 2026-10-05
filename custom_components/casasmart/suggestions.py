"""Pure, bounded contextual recommendation policy. Never executes a command."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc
MAX_RULES = 64
STATES = {
    "light": {"on", "off"},
    "switch": {"on", "off"},
    "fan": {"on", "off"},
    "binary_sensor": {"on", "off"},
    "cover": {"open", "closed"},
    "lock": {"locked", "unlocked"},
    "climate": {"off", "heat", "cool", "auto", "heat_cool", "dry", "fan_only"},
    "media_player": {"off", "on", "playing", "paused", "idle", "standby"},
}


class SuggestionError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def validate_rule(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) - {
        "rule_id",
        "scene_id",
        "enabled",
        "priority",
        "weekdays",
        "window",
        "conditions",
        "match",
    }:
        raise SuggestionError("invalid_rule")
    for field in ("rule_id", "scene_id"):
        if not isinstance(raw.get(field), str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", raw[field]
        ):
            raise SuggestionError("invalid_" + field)
    enabled, priority = raw.get("enabled", False), raw.get("priority", 0)
    days, window = raw.get("weekdays"), raw.get("window")
    if type(enabled) is not bool or not integer(priority, -100, 100):
        raise SuggestionError("invalid_rule")
    if (
        not isinstance(days, list)
        or not 1 <= len(days) <= 7
        or any(not integer(d, 0, 6) for d in days)
        or len(set(days)) != len(days)
    ):
        raise SuggestionError("invalid_weekdays")
    if not isinstance(window, dict):
        raise SuggestionError("invalid_window")
    if window.get("kind") == "fixed":
        if (
            set(window) != {"kind", "start", "end"}
            or any(
                not isinstance(window[k], str)
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", window[k])
                for k in ("start", "end")
            )
            or window["start"] == window["end"]
        ):
            raise SuggestionError("invalid_window")
    elif window.get("kind") == "sunset":
        if (
            set(window) != {"kind", "start_offset_minutes", "end_offset_minutes"}
            or not integer(window["start_offset_minutes"], -180, 180)
            or not integer(window["end_offset_minutes"], -179, 720)
            or not 0
            < window["end_offset_minutes"] - window["start_offset_minutes"]
            <= 720
        ):
            raise SuggestionError("invalid_window")
    else:
        raise SuggestionError("invalid_window")
    conditions, match = raw.get("conditions", []), raw.get("match", "all")
    if (
        match not in ("all", "any")
        or not isinstance(conditions, list)
        or len(conditions) > 16
    ):
        raise SuggestionError("invalid_conditions")
    seen = set()
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"entity_id", "state"}:
            raise SuggestionError("invalid_conditions")
        eid, state = condition["entity_id"], condition["state"]
        if (
            not isinstance(eid, str)
            or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]{1,200}", eid)
            or not isinstance(state, str)
            or state not in STATES.get(eid.split(".")[0], ())
            or eid in seen
        ):
            raise SuggestionError("invalid_conditions")
        seen.add(eid)
    return {
        "rule_id": raw["rule_id"],
        "scene_id": raw["scene_id"],
        "enabled": enabled,
        "priority": priority,
        "weekdays": sorted(days),
        "window": dict(window),
        "conditions": [dict(c) for c in conditions],
        "match": match,
    }


def _wall(day: date, value: str, zone: ZoneInfo, end: bool = False):
    naive = datetime.combine(day, time.fromisoformat(value))
    choices = [naive.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    valid = [v for v in choices if v.astimezone(zone).replace(tzinfo=None) == naive]
    # Skip a nonexistent boundary on spring-forward day; do not invent a time.
    return (max(valid) if end else min(valid)) if valid else None


def interval(rule: dict, day: date, zone: ZoneInfo, sunset: Callable):
    if day.weekday() not in rule["weekdays"]:
        return None
    window = rule["window"]
    if window["kind"] == "fixed":
        end_day = day + timedelta(days=window["end"] <= window["start"])
        start, end = (
            _wall(day, window["start"], zone),
            _wall(end_day, window["end"], zone, True),
        )
    else:
        setting = sunset(day)
        if setting is None or setting.tzinfo is None:
            return None
        start = setting.astimezone(UTC) + timedelta(
            minutes=window["start_offset_minutes"]
        )
        end = setting.astimezone(UTC) + timedelta(minutes=window["end_offset_minutes"])
    return (start, end) if start and end and start < end else None


def state_value(state):
    return (
        state.get("state") if isinstance(state, dict) else getattr(state, "state", None)
    )


def attributes(state):
    return (
        state.get("attributes", {})
        if isinstance(state, dict)
        else getattr(state, "attributes", {})
    )


def scene_satisfied(scene: dict, states: dict) -> bool:
    """Only compare fully understood absolute targets; never infer arbitrary actions."""
    if not scene.get("entities"):
        return False
    for item in scene["entities"]:
        eid, action, data = item["entity_id"], item["action"], item.get("data") or {}
        state, domain = states.get(eid), eid.split(".")[0]
        value = state_value(state)
        if value in (None, "unknown", "unavailable"):
            return False
        expected = None
        if (
            domain in {"light", "switch", "fan"}
            and action in {"turn_on", "turn_off"}
            and not data
        ):
            expected = "on" if action == "turn_on" else "off"
        elif domain == "lock" and action in {"lock", "unlock"} and not data:
            expected = "locked" if action == "lock" else "unlocked"
        elif (
            domain == "cover" and action == "set_position" and set(data) == {"position"}
        ):
            if attributes(state).get("current_position") != data["position"]:
                return False
            continue
        if expected is None or value != expected:
            return False
    return True


def evaluate(
    rule: dict,
    scene: dict | None,
    states: dict,
    now: datetime,
    zone: ZoneInfo,
    sunset: Callable,
    visible: Callable,
    *,
    policy_checks=True,
) -> tuple[dict | None, str]:
    if not rule["enabled"]:
        return None, "disabled"
    if scene is None or not scene.get("entities"):
        return None, "scene_missing"
    referenced = {c["entity_id"] for c in rule["conditions"]} | {
        i["entity_id"] for i in scene["entities"]
    }
    if not all(visible(eid) for eid in referenced):
        return None, "not_visible"
    local_day, current = now.astimezone(zone).date(), now.astimezone(UTC)
    bounds = None
    for offset in (-1, 0, 1):
        candidate = interval(rule, local_day + timedelta(days=offset), zone, sunset)
        if candidate and candidate[0] <= current < candidate[1]:
            bounds = candidate
            break
    if bounds is None:
        return None, "outside_window"
    matches = [
        state_value(states.get(c["entity_id"])) == c["state"]
        for c in rule["conditions"]
    ]
    if (
        policy_checks
        and matches
        and not (all(matches) if rule["match"] == "all" else any(matches))
    ):
        return None, "conditions_not_met"
    if policy_checks and any(
        state_value(states.get(i["entity_id"])) in (None, "unknown", "unavailable")
        for i in scene["entities"]
    ):
        return None, "scene_unavailable"
    if policy_checks and scene_satisfied(scene, states):
        return None, "already_satisfied"
    # Scene edits invalidate old occurrences too, including identical rule IDs.
    signature = json.dumps(
        [rule, scene, bounds[0].isoformat()], sort_keys=True, separators=(",", ":")
    )
    occurrence = hashlib.sha256(signature.encode()).hexdigest()
    return {
        "rule_id": rule["rule_id"],
        "scene_id": scene["scene_id"],
        "scene": {k: scene.get(k) for k in ("scene_id", "name", "icon")},
        "occurrence_id": occurrence,
        "generated_at": current.isoformat(),
        "expires_at": bounds[1].isoformat(),
        "reason": {
            "code": "time_and_state" if matches else "time_window",
            "parameters": {
                "window_kind": rule["window"]["kind"],
                "match": rule["match"],
                "condition_count": len(matches),
            },
        },
    }, "eligible"


def next_boundary(rules, now, zone, sunset):
    candidates = [now.astimezone(UTC) + timedelta(minutes=1)]
    day = now.astimezone(zone).date()
    for rule in rules:
        if not rule["enabled"]:
            continue
        for offset in (-1, 0, 1):
            bounds = interval(rule, day + timedelta(days=offset), zone, sunset)
            if bounds:
                candidates.extend(t for t in bounds if t > now)
    return min(candidates)
