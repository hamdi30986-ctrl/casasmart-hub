"""Validation and serialization behind the installer endpoints in admin_api.py.

These back the app's installer screens: Zigbee permit-join, entity rename, the
IR wizard and discovered devices. The app reaches them only through hub
endpoints gated by installer.manage, never with a Home Assistant token. No HA
imports, so the tests run without Home Assistant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# zigbee2mqtt defaults, and the permit-join window the app may ask for (seconds).
DEFAULT_ZIGBEE_BASE_TOPIC = "zigbee2mqtt"
PERMIT_JOIN_TOPIC = f"{DEFAULT_ZIGBEE_BASE_TOPIC}/bridge/request/permit_join"
DEFAULT_PERMIT_JOIN_SECONDS = 120
MIN_PERMIT_JOIN_SECONDS = 10
MAX_PERMIT_JOIN_SECONDS = 600

# The only config flows a phone may drive, for the IR wizard (Broadlink remotes
# and EasyIR climate entities). Other flows could install integrations.
ALLOWED_FLOW_HANDLERS = frozenset({"broadlink", "easy_ir"})

# Token-bearing attributes, stripped even for admins (entity_picture embeds a
# signed camera token). A denylist, because installer flows need the rest.
STRIPPED_STATE_ATTRS = frozenset({"entity_picture", "access_token", "token"})

# JSON-safe FlowResult keys the wizards read. The app drives known flows
# without rendering schemas, so data_schema and entry internals are left out.
_FLOW_RESULT_KEYS = (
    "type",
    "flow_id",
    "handler",
    "step_id",
    "errors",
    "description_placeholders",
    "reason",
    "title",
    "last_step",
)


class InstallerError(Exception):
    """Installer input rejected; the message is safe to return to the app."""


def parse_permit_join(payload: Mapping[str, Any]) -> tuple[bool, int]:
    """Validate a permit-join body into (enable, duration).

    A duration outside the permit-join window is refused, so a typo can't
    leave the Zigbee network open for hours.
    """
    enable = payload.get("enable")
    if not isinstance(enable, bool):
        raise InstallerError("enable must be true or false")
    duration = payload.get("duration", DEFAULT_PERMIT_JOIN_SECONDS)
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise InstallerError("duration must be an integer (seconds)")
    if not MIN_PERMIT_JOIN_SECONDS <= duration <= MAX_PERMIT_JOIN_SECONDS:
        raise InstallerError(
            "duration must be between "
            f"{MIN_PERMIT_JOIN_SECONDS} and {MAX_PERMIT_JOIN_SECONDS} seconds"
        )
    return enable, duration


def permit_join_payload(enable: bool, duration: int) -> str:
    """The MQTT payload for a zigbee2mqtt permit-join request."""
    if enable:
        return json.dumps({"value": True, "time": duration})
    return json.dumps({"value": False})


def permit_join_topic(base_topic: str) -> str:
    """The permit-join request topic for one zigbee2mqtt instance."""
    return f"{base_topic}/bridge/request/permit_join"


def _valid_base_topic(value: Any) -> str | None:
    """A usable zigbee2mqtt base topic, or None.

    The value comes from a phone or hub config and becomes an MQTT publish
    topic, so after trimming whitespace and outer slashes each segment may
    hold only letters, digits, _ and - (non-ASCII letters pass). Wildcards and
    empty segments are refused, so permit-join can't publish to an arbitrary
    topic.
    """
    if not isinstance(value, str):
        return None
    topic = value.strip().strip("/")
    if not all(_is_topic_segment(segment) for segment in topic.split("/")):
        return None
    return topic


def _is_topic_segment(segment: str) -> bool:
    """True for a non-empty run of letters, digits, _ and -."""
    return bool(segment) and all(ch.isalnum() or ch in "_-" for ch in segment)


def resolve_zigbee_base_topics(configured: Any, requested: Any = None) -> list[str]:
    """Which zigbee2mqtt instances a permit-join should open.

    A large home may run one instance per floor, and permit-join is per
    instance. configured is the hub's zigbee_base_topics list (the default
    topic when it has none). All of them open unless requested names one of
    them: a client may pick a configured instance but never name a new topic.
    Order is kept and duplicates are dropped.
    """
    topics: list[str] = []
    if isinstance(configured, (list, tuple)):
        for entry in configured:
            valid = _valid_base_topic(entry)
            if valid is not None and valid not in topics:
                topics.append(valid)
    if not topics:
        topics = [DEFAULT_ZIGBEE_BASE_TOPIC]

    target = _valid_base_topic(requested)
    if target is not None and target in topics:
        return [target]
    return topics


def parse_entity_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an entity-registry patch, which may only rename.

    name is a string, or null to fall back to the device name. Any other
    field is refused; this endpoint is a scoped proxy for renames.
    """
    unknown = set(payload) - {"name"}
    if unknown:
        raise InstallerError(f"Unknown field(s): {', '.join(sorted(unknown))}")
    if "name" not in payload:
        raise InstallerError("Nothing to update")
    name = payload["name"]
    if name is not None and not isinstance(name, str):
        raise InstallerError("name must be a string or null")
    return {"name": name}


def parse_remote_command(payload: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Validate an IR send body into (entity_id, commands).

    Only remote.* entities are accepted. The entity bridge does not expose the
    remote domain, which is why this endpoint exists.
    """
    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.startswith("remote."):
        raise InstallerError("entity_id must be a remote.* entity")
    command = payload.get("command")
    if isinstance(command, str) and command:
        commands = [command]
    elif (
        isinstance(command, list)
        and command
        and all(isinstance(item, str) and item for item in command)
    ):
        commands = list(command)
    else:
        raise InstallerError("command must be a non-empty string or list of strings")
    return entity_id, commands


def serialize_flow_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """A FlowResult reduced to the JSON-safe keys the wizards read."""
    out: dict[str, Any] = {}
    for key in _FLOW_RESULT_KEYS:
        value = result.get(key)
        if value is None:
            continue
        out[key] = str(value) if key == "type" else value
    return out


def serialize_progress_flow(flow: Mapping[str, Any]) -> dict[str, Any]:
    """An in-progress flow reduced to what the discovery screen reads.

    title_placeholders (Broadlink's discovery sets name, model and host) is
    returned as description_placeholders, the key the app reads from HA's
    older REST shape.
    """
    context = flow.get("context")
    context = context if isinstance(context, Mapping) else {}
    out: dict[str, Any] = {
        "flow_id": flow.get("flow_id"),
        "handler": flow.get("handler"),
        "step_id": flow.get("step_id"),
        "context": {"source": context.get("source")},
    }
    placeholders = context.get("title_placeholders")
    if isinstance(placeholders, Mapping):
        out["description_placeholders"] = dict(placeholders)
    return out


def filter_state_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """A state's attributes without the token-bearing keys."""
    return {
        key: value
        for key, value in attributes.items()
        if key not in STRIPPED_STATE_ATTRS
    }
