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
