"""Feature capabilities the hub advertises in the handshake.

The handshake stays at API v1. Apps treat a missing capability block, an
unknown feature or "available: false" as unavailable, so old hubs and apps
interoperate and a feature whose server side is incomplete fails closed.
"""

from __future__ import annotations

from typing import Final

CAPABILITY_CONTRACT_VERSION: Final = 1

# Every feature so far runs on the v1 transport. The number is explicit so a
# later feature can require a newer one without apps parsing the hub version.
_FOUNDATION_MINIMUM_API_VERSION: Final = 1

# Feature names are part of the stable handshake contract. Listing a name
# does not make it available; see _IMPLEMENTED_CAPABILITIES.
ORBIT_CAPABILITIES: Final = (
    "admin_password_v1",
    "room_activity_bulk_v1",
    "now_data_v1",
    "atomic_room_move_v1",
    "contextual_suggestions_v1",
    "generated_room_suggestions_v1",
    "push_relay_optional_v1",
    # Per-member camera access (none / rooms / all) on users, pairing codes
    # and every camera path; room ids are validated and pruned on delete.
    "member_camera_access_v1",
    # Room habits: GET/PUT /now/habits, POST /now/habits/reset (admin), and
    # GET /now?habits=household|personal.
    "now_room_habits_v1",
    # One tap "on": room on from the capture, the last-on set or the start
    # profile (lights and fans only), 409 nothing_to_turn_on + open_room,
    # rank.one_tap on the Now page, /now/rooms/{id}/start-profile.
    "now_room_one_tap_v1",
    # Room off also switches the room's running ACs, and one tap brings them
    # back from a fresh capture: an AC-only room has a working room switch.
    "now_room_off_ac_v1",
    # Generated usually_on_now plans (a room off now but usually on at this
    # hour, with its usual lights and fans) on ?kinds=usually_on_now, on the
    # household's habits with ?habits=household.
    "generated_room_habit_suggestions_v1",
    # Generated usually_off_now plans (a room's devices that are on but
    # usually switched off by now) on ?kinds=usually_off_now, same habits.
    "generated_room_habit_off_suggestions_v1",
    # ?habits_only=1 on the generated plans: only the habit kinds once the
    # caller's habits are ready (habits_ready in the answer), the others
    # only before then.
    "generated_room_habits_only_v1",
)

# Features whose server-side enforcement is complete. admin_password_v1 and
# push_relay_optional_v1 stay unavailable until theirs exists.
_IMPLEMENTED_CAPABILITIES: Final = frozenset(
    {
        "room_activity_bulk_v1",
        "now_data_v1",
        "atomic_room_move_v1",
        "contextual_suggestions_v1",
        "generated_room_suggestions_v1",
        "member_camera_access_v1",
        "now_room_habits_v1",
        "now_room_one_tap_v1",
        "now_room_off_ac_v1",
        "generated_room_habit_suggestions_v1",
        "generated_room_habit_off_suggestions_v1",
        "generated_room_habits_only_v1",
    }
)


def handshake_capabilities() -> dict[str, object]:
    """Return the capability block served by /handshake."""

    return {
        "contract_version": CAPABILITY_CONTRACT_VERSION,
        "features": {
            name: {
                "version": 1,
                "minimum_api_version": _FOUNDATION_MINIMUM_API_VERSION,
                "available": name in _IMPLEMENTED_CAPABILITIES,
            }
            for name in ORBIT_CAPABILITIES
        },
    }
