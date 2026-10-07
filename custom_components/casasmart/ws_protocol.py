"""WebSocket protocol for /api/casasmart/ws: frame parsing and building.

No HA imports, so the rules are testable without Home Assistant.

Client frames are JSON objects with a "type":
- auth {"token": ...}: the first frame, and the answer to auth_required. The
  token never goes in the URL, which ends up in logs.
- subscribe {"entity_ids": [...]}: replaces the subscription; null or omitted
  means everything the connection may see.
- ping: keep-alive (over the tunnel the app pings every 30 s).

Server frames: auth_ok, auth_failed, auth_required, subscribed (with a
snapshot), state_changed, entity_removed, pong and error, plus content-free
nudges (registry_changed, tank_changed, alarm_changed, audio_changed,
energy_changed, suggestions_changed) that make the app re-fetch over REST.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

# Frame types the client may send.
CLIENT_FRAME_TYPES: frozenset[str] = frozenset({"auth", "subscribe", "ping"})


class ProtocolError(Exception):
    """A client frame failed validation."""


def parse_client_frame(message: dict[str, Any] | Any) -> str:
    """Return the frame's type, or raise ProtocolError if it isn't a known one."""
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
    """The token from an auth frame; ProtocolError if it is missing."""
    token = message.get("token")
    if not isinstance(token, str) or not token:
        raise ProtocolError("'auth' frame requires a non-empty string 'token'")
    return token


def subscribe_entity_ids(message: dict[str, Any]) -> frozenset[str] | None:
    """The entity filter of a subscribe frame; None means everything visible."""
    entity_ids = message.get("entity_ids")
    if entity_ids is None:
        return None
    if not isinstance(entity_ids, list) or not all(
        isinstance(eid, str) and eid for eid in entity_ids
    ):
        raise ProtocolError("'entity_ids' must be null or a list of entity_id strings")
    return frozenset(entity_ids)


class Subscription:
    """What one connection has asked to receive; nothing until it subscribes."""

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
    """The token is no longer valid; a new auth frame is due within the grace."""
    return {"type": "auth_required", "grace_seconds": grace_seconds}


def frame_subscribed(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Subscription acknowledged, with a snapshot of the subscribed devices."""
    return {"type": "subscribed", "count": len(devices), "devices": devices}


def frame_state_changed(device: dict[str, Any]) -> dict[str, Any]:
    """One device changed state (same device shape as the REST API)."""
    return {"type": "state_changed", "device": device}


def frame_entity_removed(entity_id: str) -> dict[str, Any]:
    """A subscribed entity was removed, so the app can drop its tile."""
    return {"type": "entity_removed", "entity_id": entity_id}


def frame_registry_changed(kind: str) -> dict[str, Any]:
    """Floors, rooms, devices or scenes changed; the app re-fetches the registry."""
    return {"type": "registry_changed", "kind": kind}


def frame_alarm_changed() -> dict[str, Any]:
    """The alarm state changed; the app re-fetches it over REST."""
    return {"type": "alarm_changed"}


def frame_audio_changed() -> dict[str, Any]:
    """The speakers or their state changed; the app re-fetches them over REST."""
    return {"type": "audio_changed"}


def frame_energy_changed() -> dict[str, Any]:
    """Energy Saving state or config changed; the app re-fetches it over REST."""
    return {"type": "energy_changed"}


def frame_tank_changed(device_id: str) -> dict[str, Any]:
    """A tank reading arrived; the app re-fetches the level over REST."""
    return {"type": "tank_changed", "device_id": device_id}


def frame_pong() -> dict[str, Any]:
    """Reply to a client ping."""
    return {"type": "pong"}


def frame_error(message: str) -> dict[str, Any]:
    """A frame was rejected; connection stays open."""
    return {"type": "error", "message": message}


# -- Outbound backpressure -----------------------------------------------------
# A burst of pushes can outrun a slow tunnel. Rather than closing the socket,
# a newer push replaces a queued one with the same key and a full queue drops
# its oldest push. Protocol frames are never dropped.

# Push frame types that may be coalesced or dropped.
_COALESCEABLE_TYPES = frozenset(
    {
        "state_changed",
        "entity_removed",
        "registry_changed",
        "tank_changed",
        "alarm_changed",
        "audio_changed",
        "energy_changed",
    }
)


# Frames without home data, the only ones a connection awaiting re-auth gets.
# Being an allow-list, it holds back any new frame type by default.
CONTROL_FRAME_TYPES = frozenset(
    {"auth_ok", "auth_failed", "auth_required", "pong", "error"}
)


def coalesce_key(frame: dict[str, Any]) -> tuple[Any, ...] | None:
    """The key under which a newer push replaces a queued one.

    None means the frame is never coalesced or dropped.
    """
    ftype = frame.get("type")
    if ftype not in _COALESCEABLE_TYPES:
        return None
    # A change and a removal of one entity share a key, so the app never gets
    # the older of the two after the newer.
    if ftype == "state_changed":
        device = frame.get("device")
        entity_id = device.get("entity_id") if isinstance(device, dict) else None
        return ("entity", entity_id)
    if ftype == "entity_removed":
        return ("entity", frame.get("entity_id"))
    if ftype == "registry_changed":
        return ("registry_changed", frame.get("kind"))
    if ftype == "tank_changed":
        return ("tank_changed", frame.get("device_id"))
    # alarm_changed / audio_changed / energy_changed carry no payload.
    return (ftype,)


class CoalescingSendQueue:
    """Ordered outbound queue for one socket, with coalescing under pressure.

    Event-loop callbacks feed it and one writer drains it with get(), all on
    the same loop, so no lock is needed.
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
        """Queue a protocol frame; the cap doesn't apply and it is never dropped."""
        self._items.append(frame)
        self._wake()

    def offer(self, frame: dict[str, Any]) -> bool:
        """Queue a push frame, coalescing or evicting older pushes when full.

        Returns False only when the queue is full of protocol frames; the
        caller then closes the socket. A frame without a coalesce key
        (suggestions_changed) is queued normally but never replaced or evicted.
        """
        key = coalesce_key(frame)
        if key is not None:
            # Replace the queued frame in place, keeping its position.
            for i, existing in enumerate(self._items):
                if coalesce_key(existing) == key:
                    self._items[i] = frame
                    self._wake()
                    return True
        if len(self._items) < self._maxsize:
            self._items.append(frame)
            self._wake()
            return True
        # Full: evict the oldest droppable frame.
        for i, existing in enumerate(self._items):
            if coalesce_key(existing) is not None:
                del self._items[i]
                self._items.append(frame)
                self._wake()
                return True
        # Only protocol frames are queued: the consumer has stopped draining.
        return False

    def drop_data(self) -> None:
        """Discard every queued frame except control frames.

        Called when the token fails revalidation: the queued frames were built
        under claims that no longer hold.
        """
        self._items = deque(
            frame for frame in self._items if frame.get("type") in CONTROL_FRAME_TYPES
        )

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
