"""Entity bridge: which HA entities the app sees and how they are shaped.

Exposed domains, per-domain attribute allowlists, state serialization and
command validation. No Home Assistant imports, so it is unit-testable alone.
"""

from __future__ import annotations

import math
from typing import Any

# The domains the app can see at all. Anything else (remote, script, ...)
# never leaves the hub through the device API.
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
        "select",
        "number",
        "siren",
        "automation",
        "camera",
    }
)


# Per domain, the state attributes passed to the app; all others are stripped.
_ATTRIBUTE_ALLOWLIST: dict[str, frozenset[str]] = {
    "light": frozenset(
        {
            "brightness",
            "color_mode",
            "supported_color_modes",
            "rgb_color",
            "hs_color",
            "xy_color",
            "color_temp",
            "min_mireds",
            "max_mireds",
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
    "cover": frozenset(
        {
            "current_position",
            "current_tilt_position",
            "device_class",
            "supported_features",
        }
    ),
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
    "automation": frozenset({"id", "last_triggered", "mode", "current"}),
    "camera": frozenset({"brand", "model_name", "frontend_stream_type"}),
}


# Per domain: app action -> (HA service, data keys the action may carry). Only
# these commands reach Home Assistant; the values are left to the service's own
# schema.
_COMMAND_WHITELIST: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "light": {
        "turn_on": (
            "turn_on",
            frozenset(
                {
                    "brightness",
                    "brightness_pct",
                    "rgb_color",
                    "hs_color",
                    "xy_color",
                    "rgbw_color",
                    "rgbww_color",
                    "color_temp_kelvin",
                    "color_temp",
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
        "set_preset_mode": ("set_preset_mode", frozenset({"preset_mode"})),
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
            frozenset(
                {"temperature", "target_temp_low", "target_temp_high", "hvac_mode"}
            ),
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
        "turn_off": ("turn_off", frozenset()),
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
    "automation": {
        "turn_on": ("turn_on", frozenset()),
        "turn_off": ("turn_off", frozenset()),
        "trigger": ("trigger", frozenset()),
    },
}


# Exposed for reading, never commandable.
READ_ONLY_DOMAINS: frozenset[str] = frozenset({"sensor", "binary_sensor", "camera"})

# Diagnostic-category sensors and binary sensors the app shows anyway, by
# device_class: measurements and device-status flags. Other diagnostic entities
# stay hidden, except an air purifier's filter life (is_filter_life_entity).
DIAGNOSTIC_SENSOR_CLASSES: frozenset[str] = frozenset(
    {
        "power",
        "energy",
        "voltage",
        "current",
        "frequency",
        "temperature",
        "humidity",
        "battery",
    }
)


DIAGNOSTIC_BINARY_SENSOR_CLASSES: frozenset[str] = frozenset(
    {
        "tamper",
        "problem",
        "connectivity",
        "running",
    }
)


# Colour temperature. Both apps work in mireds (color_temp, min_mireds,
# max_mireds); Home Assistant takes and reports kelvin, and from 2026.1 kelvin
# only. A mired command is sent as color_temp_kelvin on every release, clamped
# to 100-1000 mireds (10000-1000 K, wider than any white light). Kelvin-only
# lights also get the mired attributes, as HA itself computed them before.
_MIRED_MIN = 100
_MIRED_MAX = 1000
_MIRED_FROM_KELVIN = (
    ("color_temp", "color_temp_kelvin"),
    ("min_mireds", "max_color_temp_kelvin"),
    ("max_mireds", "min_color_temp_kelvin"),
)


class CommandError(Exception):
    """An app command failed validation (maps to HTTP 400)."""


def entity_domain(entity_id: str) -> str:
    """Return the domain part of an entity_id ('light.living1' -> 'light')."""
    return entity_id.partition(".")[0]


def is_exposed(entity_id: str) -> bool:
    """True when the entity's domain is part of the CasaSmart surface."""
    return entity_domain(entity_id) in EXPOSED_DOMAINS


def is_filter_life_entity(entity_id: str) -> bool:
    """True for an air purifier's remaining-filter-life sensor.

    Matched by entity_id because HA publishes no device_class for it — brands
    name it ``filter_lifetime``, ``filter_life_remaining``, ``filter_remaining``.
    The app matches with the same rule.
    """
    name = entity_id.lower()
    return "filter" in name and ("life" in name or "remain" in name)


def is_category_served(category: str, entity_id: str, device_class: str | None) -> bool:
    """Category-entity exposure policy. Pure — unit-testable.

    ``category`` is the registry entity_category value (``"config"`` /
    ``"diagnostic"``); callers handle the no-category case themselves.
    """
    if category == "config":
        return True
    if category == "diagnostic":
        domain = entity_domain(entity_id)
        if domain == "sensor":
            # An air purifier's filter life is the one thing about the device
            # its owner has to act on, and HA files it under diagnostics with
            # no device_class — so the class whitelist alone would hide it.
            if is_filter_life_entity(entity_id):
                return True
            return device_class in DIAGNOSTIC_SENSOR_CLASSES
        if domain == "binary_sensor":
            return device_class in DIAGNOSTIC_BINARY_SENSOR_CLASSES
        return False
    return False


def _positive_number(value: Any) -> bool:
    """A finite number above zero (bool excluded)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _add_mired_attributes(attributes: dict[str, Any]) -> None:
    """Fill in the mired attributes a kelvin-only light lacks, in place.

    Additive: a value Home Assistant reports (None included) is never
    replaced, and nothing is derived from a missing or unset kelvin value.
    """
    for mired_key, kelvin_key in _MIRED_FROM_KELVIN:
        kelvin = attributes.get(kelvin_key)
        if mired_key not in attributes and _positive_number(kelvin):
            attributes[mired_key] = math.floor(1_000_000 / kelvin)


def _mired_to_kelvin(mireds: Any) -> int:
    """A light command's mired color_temp as kelvin, clamped to a sane range."""
    if isinstance(mireds, bool) or not isinstance(mireds, (int, float)):
        raise CommandError("'color_temp' must be a number of mireds")
    if not mireds > 0:  # also rejects NaN
        raise CommandError("'color_temp' must be above zero")
    return round(1_000_000 / min(max(mireds, _MIRED_MIN), _MIRED_MAX))


def serialize_state(
    state: Any, area: str | None = None, entity_category: str | None = None
) -> dict[str, Any]:
    """Serialize one HA state object into the CasaSmart device dict.

    ``state`` is duck-typed: needs ``entity_id``, ``state``, ``attributes``
    (mapping) and ``last_updated`` (datetime or None).
    """
    domain = entity_domain(state.entity_id)
    allowed = _ATTRIBUTE_ALLOWLIST.get(domain, frozenset())
    attributes = {
        key: value for key, value in state.attributes.items() if key in allowed
    }
    if domain == "light":
        _add_mired_attributes(attributes)
    last_updated = getattr(state, "last_updated", None)
    return {
        "entity_id": state.entity_id,
        "name": state.attributes.get("friendly_name", state.entity_id),
        "domain": domain,
        "state": state.state,
        "area": area,
        "attributes": attributes,
        "last_updated": last_updated.isoformat() if last_updated else None,
        # The app classifies config entities (settings sheets) and
        # diagnostic sensors (energy panel) by this — None for primaries.
        "entity_category": entity_category,
    }


def validate_command(
    entity_id: str, action: Any, data: Any
) -> tuple[str, str, dict[str, Any]]:
    """Validate an app command against the whitelist.

    Returns ``(ha_domain, ha_service, service_data)`` ready for
    ``hass.services.async_call``; a light's mired ``color_temp`` comes back as
    ``color_temp_kelvin``. Raises ``CommandError`` (HTTP 400 territory) on
    anything outside the whitelist.
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

    service_data = dict(data)
    if domain == "light" and "color_temp" in service_data:
        mireds = service_data.pop("color_temp")
        if "color_temp_kelvin" not in service_data:  # kelvin wins when both
            service_data["color_temp_kelvin"] = _mired_to_kelvin(mireds)
    return domain, service, service_data
