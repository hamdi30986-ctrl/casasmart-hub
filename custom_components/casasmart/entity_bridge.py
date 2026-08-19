"""CasaSmart runtime component."""

from __future__ import annotations

from typing import Any






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


    "automation": frozenset({"id", "last_triggered", "mode", "current"}),



    "camera": frozenset({"brand", "model_name", "frontend_stream_type"}),
}






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


READ_ONLY_DOMAINS: frozenset[str] = frozenset({"sensor", "binary_sensor", "camera"})


class CommandError(Exception):
    """CasaSmart runtime component."""


def entity_domain(entity_id: str) -> str:
    """CasaSmart runtime component."""
    return entity_id.partition(".")[0]


def is_exposed(entity_id: str) -> bool:
    """CasaSmart runtime component."""
    return entity_domain(entity_id) in EXPOSED_DOMAINS









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


def is_filter_life_entity(entity_id: str) -> bool:
    """CasaSmart runtime component."""
    name = entity_id.lower()
    return "filter" in name and ("life" in name or "remain" in name)


def is_category_served(
    category: str, entity_id: str, device_class: str | None
) -> bool:
    """CasaSmart runtime component."""
    if category == "config":
        return True
    if category == "diagnostic":
        domain = entity_domain(entity_id)
        if domain == "sensor":



            if is_filter_life_entity(entity_id):
                return True
            return device_class in DIAGNOSTIC_SENSOR_CLASSES
        if domain == "binary_sensor":
            return device_class in DIAGNOSTIC_BINARY_SENSOR_CLASSES
        return False
    return False


def serialize_state(
    state: Any, area: str | None = None, entity_category: str | None = None
) -> dict[str, Any]:
    """CasaSmart runtime component."""
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


        "entity_category": entity_category,
    }


def validate_command(
    entity_id: str, action: Any, data: Any
) -> tuple[str, str, dict[str, Any]]:
    """CasaSmart runtime component."""
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
