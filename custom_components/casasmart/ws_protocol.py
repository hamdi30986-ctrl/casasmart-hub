"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


CLIENT_FRAME_TYPES: frozenset[str] = frozenset({"auth", "subscribe", "ping"})


class ProtocolError(Exception):
    """CasaSmart runtime component."""


def parse_client_frame(message: dict[str, Any] | Any) -> str:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    token = message.get("token")
    if not isinstance(token, str) or not token:
        raise ProtocolError("'auth' frame requires a non-empty string 'token'")
    return token


def subscribe_entity_ids(message: dict[str, Any]) -> frozenset[str] | None:
    """CasaSmart runtime component."""
    entity_ids = message.get("entity_ids")
    if entity_ids is None:
        return None
    if not isinstance(entity_ids, list) or not all(
        isinstance(eid, str) and eid for eid in entity_ids
    ):
        raise ProtocolError("'entity_ids' must be null or a list of entity_id strings")
    return frozenset(entity_ids)


class Subscription:
    """CasaSmart runtime component."""

    def __init__(self) -> None:
        self._active = False
        self._entity_ids: frozenset[str] | None = None

    @property
    def active(self) -> bool:
        """CasaSmart runtime component."""
        return self._active

    def set(self, entity_ids: frozenset[str] | None) -> None:
        """CasaSmart runtime component."""
        self._active = True
        self._entity_ids = entity_ids

    def matches(self, entity_id: str) -> bool:
        """CasaSmart runtime component."""
        if not self._active:
            return False
        return self._entity_ids is None or entity_id in self._entity_ids





def frame_auth_ok(hub_version: str, api_version: int) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "auth_ok", "hub_version": hub_version, "api_version": api_version}


def frame_auth_failed(reason: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "auth_failed", "reason": reason}


def frame_auth_required(grace_seconds: int) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "auth_required", "grace_seconds": grace_seconds}


def frame_subscribed(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "subscribed", "count": len(devices), "devices": devices}


def frame_state_changed(device: dict[str, Any]) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "state_changed", "device": device}


def frame_entity_removed(entity_id: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "entity_removed", "entity_id": entity_id}


def frame_registry_changed(kind: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "registry_changed", "kind": kind}


def frame_alarm_changed() -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "alarm_changed"}


def frame_audio_changed() -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "audio_changed"}


def frame_energy_changed() -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "energy_changed"}


def frame_tank_changed(device_id: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "tank_changed", "device_id": device_id}


def frame_pong() -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "pong"}


def frame_error(message: str) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {"type": "error", "message": message}

















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


def coalesce_key(frame: dict[str, Any]) -> tuple[Any, ...] | None:
    """CasaSmart runtime component."""
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

    return (ftype,)


class CoalescingSendQueue:
    """CasaSmart runtime component."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._items: deque[dict[str, Any]] = deque()
        self._event = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def _wake(self) -> None:
        self._event.set()

    def put_protocol(self, frame: dict[str, Any]) -> None:
        """CasaSmart runtime component."""
        self._items.append(frame)
        self._wake()

    def offer(self, frame: dict[str, Any]) -> bool:
        """CasaSmart runtime component."""
        key = coalesce_key(frame)
        if key is not None:


            for i, existing in enumerate(self._items):
                if coalesce_key(existing) == key:
                    self._items[i] = frame
                    self._wake()
                    return True
        if len(self._items) < self._maxsize:
            self._items.append(frame)
            self._wake()
            return True

        for i, existing in enumerate(self._items):
            if coalesce_key(existing) is not None:
                del self._items[i]
                self._items.append(frame)
                self._wake()
                return True


        return False

    async def get(self) -> dict[str, Any]:
        """CasaSmart runtime component."""
        while not self._items:
            self._event.clear()



            if not self._items:
                await self._event.wait()
        return self._items.popleft()
