"""CasaSmart runtime component."""

from __future__ import annotations

import json
from typing import Any, Mapping




DEFAULT_ZIGBEE_BASE_TOPIC = "zigbee2mqtt"
PERMIT_JOIN_TOPIC = f"{DEFAULT_ZIGBEE_BASE_TOPIC}/bridge/request/permit_join"
DEFAULT_PERMIT_JOIN_SECONDS = 120
MIN_PERMIT_JOIN_SECONDS = 10
MAX_PERMIT_JOIN_SECONDS = 600





ALLOWED_FLOW_HANDLERS = frozenset({"broadlink", "easy_ir"})






STRIPPED_STATE_ATTRS = frozenset({"entity_picture", "access_token", "token"})






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
    """CasaSmart runtime component."""


def parse_permit_join(payload: Mapping[str, Any]) -> tuple[bool, int]:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    if enable:
        return json.dumps({"value": True, "time": duration})
    return json.dumps({"value": False})


def permit_join_topic(base_topic: str) -> str:
    """CasaSmart runtime component."""
    return f"{base_topic}/bridge/request/permit_join"


def _valid_base_topic(value: Any) -> str | None:
    """CasaSmart runtime component."""
    if not isinstance(value, str):
        return None
    topic = value.strip().strip("/")
    if not topic:
        return None
    segments = topic.split("/")
    if any(not seg or not _SEGMENT_OK(seg) for seg in segments):
        return None
    return topic


def _SEGMENT_OK(segment: str) -> bool:
    return all(ch.isalnum() or ch in "_-" for ch in segment)


def resolve_zigbee_base_topics(
    configured: Any, requested: Any = None
) -> list[str]:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    unknown = set(payload) - {"name"}
    if unknown:
        raise InstallerError(
            f"Unknown field(s): {', '.join(sorted(unknown))}"
        )
    changes: dict[str, Any] = {}
    if "name" in payload:
        name = payload["name"]
        if name is not None and not isinstance(name, str):
            raise InstallerError("name must be a string or null")
        changes["name"] = name
    if not changes:
        raise InstallerError("Nothing to update")
    return changes


def parse_remote_command(payload: Mapping[str, Any]) -> tuple[str, list[str]]:
    """CasaSmart runtime component."""
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
        raise InstallerError(
            "command must be a non-empty string or list of strings"
        )
    return entity_id, commands


def serialize_flow_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """CasaSmart runtime component."""
    out: dict[str, Any] = {}
    for key in _FLOW_RESULT_KEYS:
        value = result.get(key)
        if value is None:
            continue
        out[key] = str(value) if key == "type" else value
    return out


def serialize_progress_flow(flow: Mapping[str, Any]) -> dict[str, Any]:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    return {
        key: value
        for key, value in attributes.items()
        if key not in STRIPPED_STATE_ATTRS
    }
