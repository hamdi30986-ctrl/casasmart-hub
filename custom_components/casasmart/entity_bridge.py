"""Entity bridge (Track B — B1.4): HA entities → CasaSmart device model.

Pure translation layer between Home Assistant state objects and the wire
format the CasaSmart app consumes. Two jobs:

1. **Serialization** — turn an HA ``State`` into a curated device dict.
   Only domains in ``EXPOSED_DOMAINS`` are exposed, and only the
   attributes in each domain's allowlist are forwarded. The app never
   sees HA internals (integration ids, restore metadata, icon paths).
2. **Command validation** — map an app command ``{action, data}`` to an
   HA service call, rejecting anything outside the per-domain whitelist.
   The hub decides what the app may do, never the other way around.

This module deliberately imports nothing from Home Assistant — state
objects are duck-typed (``entity_id`` / ``state`` / ``attributes`` /
``last_updated``) so the logic is unit-testable without an HA install.
"""

from __future__ import annotations

from typing import Any

# -- Exposed surface -----------------------------------------------------------

# Domains the app is allowed to see. Curated to what CasaSmart actually
# renders — anything else (automation, persistent_notification, update,
# zone, ...) is hub-internal and never crosses the API.
EXPOSED_DOMAINS: frozenset[str] = frozenset(
    {
        "light",
        "switch",
        "climate",
        "cover",
        "fan",
        "lock",
        "media_player",
        "sensor",
        "binary_sensor",
        # B16 stage 3a: domains the app's settings sheets and alarm engine
        # actually drive — each ships with a strict per-action whitelist below.
        "select",
        "number",
        "siren",
    }
)

# Per-domain attribute allowlist. Forwarded verbatim when present; every
# attribute not listed here is dropped at the API boundary.
_ATTRIBUTE_ALLOWLIST: dict[str, frozenset[str]] = {
    "light": frozenset(
        {
            "brightness",
            "color_mode",
            "supported_color_modes",
            "rgb_color",
            "color_temp_kelvin",
            "min_color_temp_kelvin",
            "max_color_temp_kelvin",
            "effect",
            "effect_list",
        }
    ),
    "switch": frozenset({"device_class"}),
    "climate": frozenset(
        {
            "current_temperature",
            "temperature",
            "target_temp_low",
            "target_temp_high",
            "hvac_modes",
            "hvac_action",
            "fan_mode",
            "fan_modes",
            "min_temp",
            "max_temp",
        }
    ),
    "cover": frozenset({"current_position", "current_tilt_position", "device_class"}),
    "fan": frozenset({"percentage", "percentage_step", "preset_mode", "preset_modes"}),
    "lock": frozenset({}),
    "media_player": frozenset(
        {
            "volume_level",
            "is_volume_muted",
            "media_title",
            "media_artist",
            "source",
            "source_list",
        }
    ),
    "sensor": frozenset({"unit_of_measurement", "device_class", "state_class"}),
    "binary_sensor": frozenset({"device_class"}),
    "select": frozenset({"options"}),
    "number": frozenset({"min", "max", "step", "unit_of_measurement"}),
    "siren": frozenset({"device_class"}),
}

# -- Command whitelist ---------------------------------------------------------

# action -> (HA service name, allowed service-data keys). An action absent
# from its domain's table is rejected; a data key absent from the action's
# allowed set is rejected. Empty set = the action takes no data.
_COMMAND_WHITELIST: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "light": {
        "turn_on": (
            "turn_on",
            frozenset(
                {
                    "brightness",
                    "brightness_pct",
                    "rgb_color",
                    "color_temp_kelvin",
                    "effect",
                    "transition",
                }
            ),
        ),
        "turn_off": ("turn_off", frozenset({"transition"})),
        "toggle": ("toggle", frozenset()),
    },
    "switch": {
        "turn_on": ("turn_on", frozenset()),
        "turn_off": ("turn_off", frozenset()),
        "toggle": ("toggle", frozenset()),
    },
    "fan": {
        "turn_on": ("turn_on", frozenset({"percentage", "preset_mode"})),
        "turn_off": ("turn_off", frozenset()),
        "set_percentage": ("set_percentage", frozenset({"percentage"})),
    },
    "cover": {
        "open": ("open_cover", frozenset()),
        "close": ("close_cover", frozenset()),
        "stop": ("stop_cover", frozenset()),
        "set_position": ("set_cover_position", frozenset({"position"})),
        "set_tilt_position": ("set_cover_tilt_position", frozenset({"tilt_position"})),
    },
    "climate": {
        "set_temperature": (
            "set_temperature",
            frozenset({"temperature", "target_temp_low", "target_temp_high"}),
        ),
        "set_hvac_mode": ("set_hvac_mode", frozenset({"hvac_mode"})),
        "set_fan_mode": ("set_fan_mode", frozenset({"fan_mode"})),
        "turn_off": ("turn_off", frozenset()),
    },
    "lock": {
        "lock": ("lock", frozenset()),
        "unlock": ("unlock", frozenset()),
    },
    "media_player": {
        "play": ("media_play", frozenset()),
        "pause": ("media_pause", frozenset()),
        "set_volume": ("volume_set", frozenset({"volume_level"})),
        "mute": ("volume_mute", frozenset({"is_volume_muted"})),
    },
    "select": {
        "select_option": ("select_option", frozenset({"option"})),
    },
    "number": {
        "set_value": ("set_value", frozenset({"value"})),
    },
    "siren": {
        "turn_on": ("turn_on", frozenset()),
        "turn_off": ("turn_off", frozenset()),
    },
}

# Domains that are exposed read-only (no commands ever).
READ_ONLY_DOMAINS: frozenset[str] = frozenset({"sensor", "binary_sensor"})


class CommandError(Exception):
    """An app command failed validation (maps to HTTP 400)."""


def entity_domain(entity_id: str) -> str:
    """Return the domain part of an entity_id ('light.living1' -> 'light')."""
    return entity_id.partition(".")[0]


def is_exposed(entity_id: str) -> bool:
    """True when the entity's domain is part of the CasaSmart surface."""
    return entity_domain(entity_id) in EXPOSED_DOMAINS


def serialize_state(state: Any, area: str | None = None) -> dict[str, Any]:
    """Serialize one HA state object into the CasaSmart device dict.

    ``state`` is duck-typed: needs ``entity_id``, ``state``, ``attributes``
    (mapping) and ``last_updated`` (datetime or None).
    """
    domain = entity_domain(state.entity_id)
    allowed = _ATTRIBUTE_ALLOWLIST.get(domain, frozenset())
    attributes = {
        key: value for key, value in state.attributes.items() if key in allowed
    }
    last_updated = getattr(state, "last_updated", None)
    return {
        "entity_id": state.entity_id,
        "name": state.attributes.get("friendly_name", state.entity_id),
        "domain": domain,
        "state": state.state,
        "area": area,
        "attributes": attributes,
        "last_updated": last_updated.isoformat() if last_updated else None,
    }


def validate_command(
    entity_id: str, action: Any, data: Any
) -> tuple[str, str, dict[str, Any]]:
    """Validate an app command against the whitelist.

    Returns ``(ha_domain, ha_service, service_data)`` ready for
    ``hass.services.async_call``. Raises ``CommandError`` (HTTP 400
    territory) on anything outside the whitelist.
    """
    domain = entity_domain(entity_id)
    if domain not in EXPOSED_DOMAINS:
        raise CommandError(f"Domain {domain!r} is not exposed")
    if domain in READ_ONLY_DOMAINS:
        raise CommandError(f"Domain {domain!r} is read-only")

    if not isinstance(action, str) or not action:
        raise CommandError("'action' must be a non-empty string")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise CommandError("'data' must be an object")

    actions = _COMMAND_WHITELIST.get(domain, {})
    if action not in actions:
        allowed_actions = ", ".join(sorted(actions)) or "none"
        raise CommandError(
            f"Action {action!r} not allowed for {domain!r} (allowed: {allowed_actions})"
        )

    service, allowed_keys = actions[action]
    rejected = set(data) - allowed_keys
    if rejected:
        raise CommandError(
            f"Data keys not allowed for {action!r}: {', '.join(sorted(rejected))}"
        )

    return domain, service, dict(data)
