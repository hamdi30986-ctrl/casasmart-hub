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

import asyncio
from collections import deque
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


def frame_entity_removed(entity_id: str) -> dict[str, Any]:
    """A subscribed entity was removed (unpaired / integration drop / registry
    delete). Carries only the id so the app drops the tile instead of leaving a
    dead card until the next reconnect — no state to scope, just the id."""
    return {"type": "entity_removed", "entity_id": entity_id}


def frame_registry_changed(kind: str) -> dict[str, Any]:
    """The home's organization changed (B17: floors/rooms/devices/scenes).
    Deliberately content-free — the app re-fetches through its own scoped
    registry GET, so the push can never leak what REST would hide."""
    return {"type": "registry_changed", "kind": kind}


def frame_alarm_changed() -> dict[str, Any]:
    """The arm state changed (B13: arm/disarm/pending/triggered/tamper).
    Content-free like ``registry_changed`` — the app re-fetches through the
    permission-gated alarm state GET, so the push leaks nothing even though
    the socket authorized only on ``devices.read``. The server additionally
    only sends this to connections whose role carries ``alarm.read``."""
    return {"type": "alarm_changed"}


def frame_audio_changed() -> dict[str, Any]:
    """The hub's speaker view changed (B14: enroll/rename/drop or a live
    status/state ingest off the bus). Content-free like ``alarm_changed`` —
    the app re-fetches through the ``audio.read`` gated speakers GET, so the
    push leaks nothing. The server only sends this to connections whose role
    carries ``audio.read``."""
    return {"type": "audio_changed"}


def frame_tank_changed(device_id: str) -> dict[str, Any]:
    """A tank reading landed (Phase 4 ingest). Carries only the device id —
    content-free like ``registry_changed``; the app re-fetches the calibrated
    level through its tank GET, so the push leaks nothing REST would hide."""
    return {"type": "tank_changed", "device_id": device_id}


def frame_pong() -> dict[str, Any]:
    """Reply to a client ping."""
    return {"type": "pong"}


def frame_error(message: str) -> dict[str, Any]:
    """A frame was rejected; connection stays open."""
    return {"type": "error", "message": message}


# -- Outbound backpressure (Phase 11) -----------------------------------------
# On a congested tunnel (LTE, weak WiFi) a burst of state changes — a scene
# flipping 20 lights, a re-sync fan-out — can arrive faster than the socket
# drains. The old queue force-closed (4003 "too slow") the moment it hit its
# cap, killing a perfectly healthy app that would have caught up in a second.
#
# These "push" frames are loss-tolerant: only the LATEST state of an entity
# matters, and a nudge (registry/tank/alarm/audio) just says "re-fetch", so a
# duplicate is pure redundancy. So under pressure we COALESCE (newer frame
# replaces the older for the same entity/nudge) and, only if still over the cap,
# DROP THE OLDEST push frame — never the socket. Protocol frames (auth_ok,
# subscribed, auth_required/failed, error, pong) are loss-INTOLERANT: dropping
# one corrupts the session, so they are always admitted and never evicted.

# Push frame types whose older copies are safe to coalesce/drop under pressure.
_COALESCEABLE_TYPES = frozenset(
    {
        "state_changed",
        "entity_removed",
        "registry_changed",
        "tank_changed",
        "alarm_changed",
        "audio_changed",
    }
)


def coalesce_key(frame: dict[str, Any]) -> tuple[Any, ...] | None:
    """Identity under which two frames are redundant, or None to never drop.

    Two queued frames with the same key mean the same thing to the app — the
    newer one fully supersedes the older (latest entity state, or an identical
    "re-fetch" nudge). None marks a loss-intolerant protocol frame that must
    never be coalesced or dropped.
    """
    ftype = frame.get("type")
    if ftype not in _COALESCEABLE_TYPES:
        return None
    if ftype == "state_changed":
        device = frame.get("device")
        entity_id = device.get("entity_id") if isinstance(device, dict) else None
        return ("state_changed", entity_id)
    if ftype == "entity_removed":
        return ("entity_removed", frame.get("entity_id"))
    if ftype == "registry_changed":
        return ("registry_changed", frame.get("kind"))
    if ftype == "tank_changed":
        return ("tank_changed", frame.get("device_id"))
    # alarm_changed / audio_changed carry no payload — one identity each.
    return (ftype,)


class CoalescingSendQueue:
    """Order-preserving single-consumer outbound queue with backpressure relief.

    One writer drains it via :meth:`get`; the HA event-loop callbacks feed it.
    Both run on the same loop, so no lock is needed — the only ``await`` is in
    :meth:`get`, and nothing mutates the queue across it except synchronous
    producer callbacks.

    Protocol frames go in via :meth:`put_protocol` (always admitted). Push
    frames go in via :meth:`offer`, which returns ``False`` only when the queue
    is full of undroppable protocol frames — the genuine "hopeless consumer"
    case where the caller should close the socket.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._items: deque[dict[str, Any]] = deque()
        self._event = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def _wake(self) -> None:
        self._event.set()

    def put_protocol(self, frame: dict[str, Any]) -> None:
        """Admit a loss-intolerant protocol frame unconditionally (never
        coalesced, never dropped — the cap does not apply to it)."""
        self._items.append(frame)
        self._wake()

    def offer(self, frame: dict[str, Any]) -> bool:
        """Admit a push frame, coalescing/dropping under pressure.

        Returns True when the frame (or a newer equivalent) is queued, False
        only when the queue is full of undroppable protocol frames — then the
        caller closes the socket. A None-key frame passed here is treated as
        droppable-if-over-cap but never coalesced (defensive; callers pass real
        push frames).
        """
        key = coalesce_key(frame)
        if key is not None:
            # Coalesce: a newer frame for the same entity/nudge replaces the
            # queued one in place — no growth, no reorder across other entities.
            for i, existing in enumerate(self._items):
                if coalesce_key(existing) == key:
                    self._items[i] = frame
                    self._wake()
                    return True
        if len(self._items) < self._maxsize:
            self._items.append(frame)
            self._wake()
            return True
        # Over the cap: evict the OLDEST droppable frame to admit this one.
        for i, existing in enumerate(self._items):
            if coalesce_key(existing) is not None:
                del self._items[i]
                self._items.append(frame)
                self._wake()
                return True
        # Nothing droppable — the whole backlog is protocol frames the consumer
        # isn't draining. That IS a dead/hopeless link; let the caller close.
        return False

    async def get(self) -> dict[str, Any]:
        """Pop the oldest frame, waiting until one is available."""
        while not self._items:
            self._event.clear()
            # Re-check after clear: a producer that appended between the empty
            # check and the clear already fired _wake, which the clear would
            # otherwise swallow — this guard stops a lost-wakeup hang.
            if not self._items:
                await self._event.wait()
        return self._items.popleft()
