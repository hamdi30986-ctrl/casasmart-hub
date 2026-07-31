"""HA-free cross-validation of Energy Saving wizard picks and discovery."""

from __future__ import annotations

import math
from typing import Any

try:
    from .energy import LEVEL_LOW, LEVEL_MEDIUM, LEVEL_SMART, EnergyConfigError
except ImportError:  # direct module import in the HA-free unit environment
    from energy import (  # type: ignore[no-redef]
        LEVEL_LOW,
        LEVEL_MEDIUM,
        LEVEL_SMART,
        EnergyConfigError,
    )


def validate_config_against_discovery(
    level: str, config: dict[str, Any], discovery: dict[str, Any]
) -> None:
    """Reject incomplete, stale, or foreign picks before durable storage."""
    rooms = {room["room_id"]: room for room in discovery["rooms"]}
    excluded = set(config["excluded_rooms"])
    unknown_excluded = excluded - set(rooms)
    if unknown_excluded:
        raise EnergyConfigError(f"unknown excluded rooms: {sorted(unknown_excluded)}")

    groups = {
        gang["group_id"]: (room_id, gang)
        for room_id, room in rooms.items()
        for gang in room["gangs"]
    }
    allowed_counts = {3} if level == LEVEL_LOW else {2, 3}
    eligible_groups = {
        group_id
        for group_id, (room_id, gang) in groups.items()
        if room_id not in excluded and gang["channel_count"] in allowed_counts
    }
    for group_id, picks in config["gang_keepers"].items():
        if group_id not in groups:
            raise EnergyConfigError(f"unknown gang group {group_id!r}")
        if groups[group_id][0] in excluded:
            raise EnergyConfigError(
                f"gang_keepers.{group_id} belongs to an excluded room"
            )
        candidates = {item["entity_id"] for item in groups[group_id][1]["channels"]}
        if not set(picks).issubset(candidates):
            raise EnergyConfigError(f"gang_keepers.{group_id} contains a stale pick")
    if config["setup_complete"] and set(config["gang_keepers"]) != eligible_groups:
        raise EnergyConfigError("gang keeper setup is incomplete or stale")

    eligible_light_rooms: set[str] = set()
    for room_id, room in rooms.items():
        candidates = {item["entity_id"] for item in room["lights"]}
        if room_id not in excluded and len(candidates) > 1 and not (
            level == LEVEL_SMART and room["automatic"]
        ):
            eligible_light_rooms.add(room_id)
        if room_id not in config["light_keepers"]:
            continue
        picks = config["light_keepers"][room_id]
        if not set(picks).issubset(candidates):
            raise EnergyConfigError(f"light_keepers.{room_id} contains a stale pick")
        expected = math.ceil(len(candidates) / 2)
        if len(picks) != expected:
            raise EnergyConfigError(
                f"light_keepers.{room_id} must contain exactly {expected} keepers"
            )
    if (
        config["setup_complete"]
        and set(config["light_keepers"]) != eligible_light_rooms
    ):
        raise EnergyConfigError("light keeper setup is incomplete or stale")

    plugs = {
        item["entity_id"]
        for room in rooms.values()
        if room["room_id"] not in excluded
        for item in room["plugs"]
    }
    if not set(config["plug_offs"]).issubset(plugs):
        raise EnergyConfigError("plug_offs contains a stale or non-plug entity")

    heaters = {
        item["entity_id"]
        for room in rooms.values()
        if room["room_id"] not in excluded
        for item in room["heaters"]
    }
    if not {item["entity_id"] for item in config["heaters"]}.issubset(heaters):
        raise EnergyConfigError("heaters contains a stale or non-heater entity")
    if config["setup_complete"] and {
        item["entity_id"] for item in config["heaters"]
    } != heaters:
        raise EnergyConfigError("heater setup is incomplete or stale")

    eligible_ac_rooms: set[str] = set()
    for room_id, room in rooms.items():
        candidates = {item["entity_id"] for item in room["climates"]}
        if room_id not in excluded and (
            (level == LEVEL_MEDIUM and len(candidates) > 1)
            or (
                level == LEVEL_SMART
                and not room["automatic"]
                and bool(candidates)
            )
        ):
            eligible_ac_rooms.add(room_id)
        if room_id in config["ac_keepers"] and not set(
            config["ac_keepers"][room_id]
        ).issubset(candidates):
            raise EnergyConfigError(f"ac_keepers.{room_id} contains a stale pick")
    if config["setup_complete"] and set(config["ac_keepers"]) != eligible_ac_rooms:
        raise EnergyConfigError("AC setup is incomplete or stale")
