"""Versioned, additive feature-capability contract for CasaSmart clients.

The handshake remains API v1.  Clients must treat a missing capability block,
an unknown capability, or ``available: false`` as unavailable.  This lets old
Hubs and clients interoperate while later protected features can fail closed.
"""

from __future__ import annotations

from typing import Final


CAPABILITY_CONTRACT_VERSION: Final = 1

# All foundation features are defined against the existing v1 transport.  The
# number is explicit so a future feature can require a newer transport without
# clients having to infer support from a Hub release string.
_FOUNDATION_MINIMUM_API_VERSION: Final = 1

# These names are part of the public, stable handshake contract.  A feature is
# deliberately not enabled merely because a Hub advertises its name; later
# phases must set ``available`` only when the complete server-side policy is
# enforcing it.
ORBIT_CAPABILITIES: Final = (
    "admin_password_v1",
    "room_activity_bulk_v1",
    "now_data_v1",
    "atomic_room_move_v1",
    "contextual_suggestions_v1",
    "generated_room_suggestions_v1",
    "push_relay_optional_v1",
)

# These capability values are an endpoint-completeness gate, not a roadmap.
# Keep protected admin-password and optional-relay support fail-closed until
# their own server-side enforcement exists.
_IMPLEMENTED_CAPABILITIES: Final = frozenset(
    {
        "room_activity_bulk_v1", "now_data_v1", "atomic_room_move_v1",
        "contextual_suggestions_v1",
        "generated_room_suggestions_v1",
    }
)


def handshake_capabilities() -> dict[str, object]:
    """Return the safe baseline capability block for ``/handshake``.

    Only complete server endpoints are available.  Clients must not infer
    security or relay support from the Hub API version alone.
    """

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
