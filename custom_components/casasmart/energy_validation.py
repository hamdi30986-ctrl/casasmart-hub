"""Checks Energy Saving wizard picks against the live discovery (no HA imports).

energy.validate_level_config checks a level's document on its own; this
checks it against the energy_api discovery payload. energy_api runs it on
every wizard PATCH, before anything is stored.
"""

from __future__ import annotations

import math
from typing import Any

try:
    from .energy import LEVEL_LOW, LEVEL_MEDIUM, LEVEL_SMART, EnergyConfigError
except ImportError:  # imported as a top-level module by the HA-free unit tests
    from energy import (  # type: ignore[no-redef]
        LEVEL_LOW,
        LEVEL_MEDIUM,
        LEVEL_SMART,
        EnergyConfigError,
    )


def validate_config_against_discovery(
    level: str, config: dict[str, Any], discovery: dict[str, Any]
) -> None:
    """Reject incomplete, stale, or foreign picks before durable storage.

    Every pick must be a current candidate in a room that is not excluded.
    Low keeps two channels of each three-gang switch, Medium and Smart one of
    each two- or three-gang switch; a room with several lights keeps
    ceil(n/2) of them. Once setup_complete is set, every eligible gang, light
    room, heater and AC room must have an answer. Raises EnergyConfigError
    naming the first problem.
    """
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
    # Low only thins three-gang switches; a two-gang keeps both its channels.
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
        # Smart drives the lights itself in a room with both sensors.
        if (
            room_id not in excluded
            and len(candidates) > 1
            and not (level == LEVEL_SMART and room["automatic"])
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
    picked_heaters = {item["entity_id"] for item in config["heaters"]}
    if not picked_heaters.issubset(heaters):
        raise EnergyConfigError("heaters contains a stale or non-heater entity")
    if config["setup_complete"] and picked_heaters != heaters:
        raise EnergyConfigError("heater setup is incomplete or stale")

    # Medium asks which AC to keep in a room with several; Smart asks for
    # every sensorless room with an AC (it may keep none of them).
    eligible_ac_rooms: set[str] = set()
    for room_id, room in rooms.items():
        candidates = {item["entity_id"] for item in room["climates"]}
        if room_id not in excluded and (
            (level == LEVEL_MEDIUM and len(candidates) > 1)
            or (level == LEVEL_SMART and not room["automatic"] and bool(candidates))
        ):
            eligible_ac_rooms.add(room_id)
        if room_id in config["ac_keepers"] and not set(
            config["ac_keepers"][room_id]
        ).issubset(candidates):
            raise EnergyConfigError(f"ac_keepers.{room_id} contains a stale pick")
    if config["setup_complete"] and set(config["ac_keepers"]) != eligible_ac_rooms:
        raise EnergyConfigError("AC setup is incomplete or stale")
