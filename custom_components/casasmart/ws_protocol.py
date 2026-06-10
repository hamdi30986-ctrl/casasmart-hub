"""WebSocket wire protocol (Track B — B1.5): frame parsing + building.

Pure protocol layer for the CasaSmart WebSocket server. Like
``entity_bridge``, this module imports nothing from Home Assistant so the
protocol rules are unit-testable without an HA install.

Client -> server frames (JSON objects, ``type`` discriminator):

- ``{"type": "auth", "token": "<jwt>"}`` — MUST be the first frame after
  connect (plan: token never in the URL — URLs leak into logs). Also the
  answer to a mid-connection ``auth_required``.
- ``{"type": "subscribe", "entity_ids": [...]}`` — start receiving state
  pushes. ``entity_ids`` omitted or null = everything the connection is
  allowed to see. Replaces any previous subscription.
- ``{"type": "ping"}`` — app-level keep-alive (plan B16: tunnel mode pings
  every 30s to stop Cloudflare dropping idle connections).

Server -> client frames:

- ``auth_ok`` / ``auth_failed`` / ``auth_required``
- ``subscribed`` — subscription acknowledged, with device snapshot
- ``state_changed`` — one device changed (same wire shape as REST)
- ``pong`` / ``error``
"""

from __future__ import annotations

from typing import Any

# Frame types the client may send.
CLIENT_FRAME_TYPES: frozenset[str] = frozenset({"auth", "subscribe", "ping"})


class ProtocolError(Exception):
    """A client frame failed validation (server replies ``error`` or closes)."""


def parse_client_frame(message: dict[str, Any] | Any) -> str:
    """Validate the basic shape of a client frame, return its type.

    Raises ``ProtocolError`` unless the frame is a JSON object whose
    ``type`` is a known client frame type.
    """
    if not isinstance(message, dict):
        raise ProtocolError("Frame must be a JSON object")
    frame_type = message.get("type")
    if not isinstance(frame_type, str) or not frame_type:
        raise ProtocolError("Frame must have a string 'type'")
    if frame_type not in CLIENT_FRAME_TYPES:
        allowed = ", ".join(sorted(CLIENT_FRAME_TYPES))
        raise ProtocolError(f"Unknown frame type {frame_type!r} (allowed: {allowed})")
    return frame_type


def auth_token(message: dict[str, Any]) -> str:
    """Extract the token from an ``auth`` frame, or raise ``ProtocolError``."""
    token = message.get("token")
    if not isinstance(token, str) or not token:
        raise ProtocolError("'auth' frame requires a non-empty string 'token'")
    return token


def subscribe_entity_ids(message: dict[str, Any]) -> frozenset[str] | None:
    """Extract the entity filter from a ``subscribe`` frame.

    Returns ``None`` for "everything visible to this connection" (key
    omitted or null), else a frozenset of entity_ids. Raises
    ``ProtocolError`` on anything malformed.
    """
    entity_ids = message.get("entity_ids")
    if entity_ids is None:
        return None
    if not isinstance(entity_ids, list) or not all(
        isinstance(eid, str) and eid for eid in entity_ids
    ):
        raise ProtocolError("'entity_ids' must be null or a list of entity_id strings")
    return frozenset(entity_ids)


class Subscription:
    """What one connection has asked to receive.

    Starts empty: nothing is pushed until the client sends ``subscribe``
    (plan: pushes go to subscribed entities only).
    """

    def __init__(self) -> None:
        self._active = False
        self._entity_ids: frozenset[str] | None = None

    @property
    def active(self) -> bool:
        """True once the client has subscribed."""
        return self._active

    def set(self, entity_ids: frozenset[str] | None) -> None:
        """Replace the subscription (None = all visible entities)."""
        self._active = True
        self._entity_ids = entity_ids

    def matches(self, entity_id: str) -> bool:
        """True when a state change for entity_id should be pushed."""
        if not self._active:
            return False
        return self._entity_ids is None or entity_id in self._entity_ids


# -- Server frame builders -----------------------------------------------------


def frame_auth_ok(hub_version: str, api_version: int) -> dict[str, Any]:
    """Auth accepted; connection is live."""
    return {"type": "auth_ok", "hub_version": hub_version, "api_version": api_version}


def frame_auth_failed(reason: str) -> dict[str, Any]:
    """Auth rejected; server closes after sending this."""
    return {"type": "auth_failed", "reason": reason}


def frame_auth_required(grace_seconds: int) -> dict[str, Any]:
    """Token no longer valid mid-connection; fresh auth frame expected
    within the grace window or the server closes (plan: 30s grace)."""
    return {"type": "auth_required", "grace_seconds": grace_seconds}


def frame_subscribed(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Subscription acknowledged + current snapshot of the subscribed set,
    so the app starts from known state instead of waiting for changes."""
    return {"type": "subscribed", "count": len(devices), "devices": devices}


def frame_state_changed(device: dict[str, Any]) -> dict[str, Any]:
    """One device changed state — same device shape as the REST API."""
    return {"type": "state_changed", "device": device}


def frame_pong() -> dict[str, Any]:
    """Reply to a client ping."""
    return {"type": "pong"}


def frame_error(message: str) -> dict[str, Any]:
    """A frame was rejected; connection stays open."""
    return {"type": "error", "message": message}
