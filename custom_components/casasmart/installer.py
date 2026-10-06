"""Installer-surface helpers (Track B — B16 3c-4b): the pure logic
behind the admin endpoints that replace the app's raw-HA-token installer
calls (pairing sheet, switch domain swap, IR wizard, discovered devices).

No HA imports — unit-testable without an HA install, exactly like
``entity_bridge``, ``camera_streams`` and ``pairing``. The views in
``admin_api.py`` are thin wrappers over these functions.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# zigbee2mqtt's permit-join request topic. The app used to publish here
# itself through ``mqtt.publish`` with the raw HA token — the hub now
# owns the publish behind ``installer.manage``.
DEFAULT_ZIGBEE_BASE_TOPIC = "zigbee2mqtt"
PERMIT_JOIN_TOPIC = f"{DEFAULT_ZIGBEE_BASE_TOPIC}/bridge/request/permit_join"
DEFAULT_PERMIT_JOIN_SECONDS = 120
MIN_PERMIT_JOIN_SECONDS = 10
MAX_PERMIT_JOIN_SECONDS = 600

# Config-flow handlers the proxy will drive — and the COMPLETE list of
# them. The IR wizard accepts DHCP-discovered Broadlink remotes and
# creates EasyIR climate entities; nothing else on the hub may be
# config-flowed from a phone (a flow can install integrations).
ALLOWED_FLOW_HANDLERS = frozenset({"broadlink", "easy_ir"})

# State attributes that never cross the API boundary, even on the
# admin-only raw dump: ``entity_picture`` embeds an HA-signed camera
# token (the 3c-3 doctrine) and ``access_token``/``token`` are exactly
# what they say. Same reasoning as the entity-bridge allowlist, applied
# as a denylist because installer flows need everything else verbatim.
STRIPPED_STATE_ATTRS = frozenset({"entity_picture", "access_token", "token"})

# FlowResult keys that are JSON-safe AND that the installer wizards
# read. Deliberately excludes ``data_schema`` (voluptuous objects),
# ``context``, ``data``/``result`` (config-entry internals) and
# ``preview`` — the app drives known flows blind, it never renders
# schemas.
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
    """Installer input rejected — message is safe to return verbatim."""


def parse_permit_join(payload: Mapping[str, Any]) -> tuple[bool, int]:
    """Validate a permit-join request body into ``(enable, duration)``.

    ``duration`` only matters when enabling; it is clamped to a sane
    window so a typo can't leave the Zigbee network open for hours.
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
    """The exact MQTT payload zigbee2mqtt expects (what the app sent)."""
    if enable:
        return json.dumps({"value": True, "time": duration})
    return json.dumps({"value": False})


def permit_join_topic(base_topic: str) -> str:
    """The permit-join request topic for one zigbee2mqtt instance."""
    return f"{base_topic}/bridge/request/permit_join"


def _valid_base_topic(value: Any) -> str | None:
    """A usable z2m ``base_topic``, or None when it isn't one.

    Deliberately strict. The value ends up as an MQTT publish topic, and the
    caller is a phone (admin/sub-admin) or hub config — so wildcards (``+``,
    ``#``), empty segments and leading/trailing slashes are refused rather
    than normalised, and nothing outside ``[A-Za-z0-9_-]`` per segment is
    accepted. That keeps a typo (or a hostile value) from turning permit-join
    into a publish to an arbitrary topic.
    """
    if not isinstance(value, str):
        return None
    topic = value.strip().strip("/")
    if not topic:
        return None
    segments = topic.split("/")
    if any(not seg or not _SEGMENT_OK(seg) for seg in segments):
        return None
    return topic


def _SEGMENT_OK(segment: str) -> bool:  # noqa: N802 — module-private predicate
    return all(ch.isalnum() or ch in "_-" for ch in segment)


def resolve_zigbee_base_topics(
    configured: Any, requested: Any = None
) -> list[str]:
    """Which zigbee2mqtt instances a permit-join should open.

    A villa commonly runs two or three zigbee2mqtt instances (one coordinator
    per floor, sometimes per wing). permit_join is per-instance: publishing
    only to the default ``zigbee2mqtt`` base topic opens floor 1's coordinator
    and leaves every other one shut, so devices on the other floors can never
    be paired from the app. That was the behaviour until this function existed.

    * ``configured`` — the hub's ``zigbee_base_topics`` list. With no request
      target, EVERY configured instance is opened, so an unmodified app that
      sends no target now reaches all the coordinators instead of one.
    * ``requested`` — a single base topic from the request body, for a client
      that knows which instance it wants (floor -> instance mapping). It must
      name one of the hub's KNOWN instances: a client may pick among the
      coordinators the installer declared, never invent a topic. Anything
      else falls back to opening them all, because the safe failure for
      "add a device" is every coordinator listening, not none.
    * Neither -> the single default topic, i.e. exactly the old behaviour on
      a hub that was never configured for multi-instance.

    Order is preserved and duplicates collapse, so the caller publishes once
    per real instance.
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
    """Validate an entity-registry patch body — a name rename only.

    The switch_as_x domain swap (``options_domain``/``options``) is gone
    (Phase 7): the app never re-domains an entity, because a gang's type is
    presentation metadata, never an HA rename. The only operation left is:

    - ``name`` — rename (string, or null to clear back to the device name)

    Anything else is rejected: this endpoint is a scoped proxy, not a
    general registry editor.
    """
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
    """Validate an IR send body into ``(entity_id, commands)``.

    Only ``remote.*`` entities are addressable — this endpoint exists
    solely because the remote domain is (deliberately) not on the
    entity-bridge whitelist.
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
        raise InstallerError(
            "command must be a non-empty string or list of strings"
        )
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

    ``title_placeholders`` (set by e.g. Broadlink's DHCP discovery step:
    name / model / host) is surfaced as ``description_placeholders`` —
    the key the app already parses from HA's legacy REST shape.
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
    """A state's attributes minus the token-bearing keys (see above)."""
    return {
        key: value
        for key, value in attributes.items()
        if key not in STRIPPED_STATE_ATTRS
    }
