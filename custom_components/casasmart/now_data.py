"""Hub-owned state behind the Now page, and the rules for room commands.

NowDataEngine keeps, each in its own table: each member's recently
controlled devices (recorded after a command succeeds), each room's activity
policy, the Now configuration, recent room-command answers for idempotent
retries, and each room's restore set (what its last off switched off). The
functions decide which devices may join a room command. Storage methods are
synchronous; now_api runs them in the executor.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

_MAX_RECENTS = 48
_MAX_PINNED_MOMENTS = 12
_MAX_CONTACTS = 64
# Stored room-command answers per member; the oldest is dropped first.
_MAX_IDEMPOTENCY_ENTRIES = 64
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# Domains a room command may switch.
_ROOM_ACTIVITY_DOMAINS = frozenset({"light", "fan", "switch"})
# A switch without a device class could be anything, so only switches with
# device class "switch" may join a room command.
_SAFE_SWITCH_DEVICE_CLASSES = frozenset({"switch"})


class NowDataError(Exception):
    """A Now configuration, policy or command value is invalid."""


def _optional_entity_id(value: Any, field: str) -> str | None:
    """An entity-id-shaped string, or None."""
    if value is None:
        return None
    if not isinstance(value, str) or "." not in value or len(value) > 255:
        raise NowDataError(f"{field} must be an entity_id string or null")
    return value


def _timestamp(value: datetime | None = None) -> str:
    """The time (default: now) as an ISO 8601 UTC string."""
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _state_value(state: Any, field: str, default: Any = None) -> Any:
    """Read a field from an HA State or from a plain dict."""
    if isinstance(state, dict):
        return state.get(field, default)
    return getattr(state, field, default)


def _state_attributes(state: Any) -> dict[str, Any]:
    attributes = _state_value(state, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


def is_room_activity_candidate(state: Any) -> bool:
    """True when an admin may approve this device for room commands."""
    entity_id = _state_value(state, "entity_id", "")
    domain = entity_id.split(".", 1)[0]
    if domain not in _ROOM_ACTIVITY_DOMAINS:
        return False
    attributes = _state_attributes(state)
    if attributes.get("casasmart_room_activity_exclude") is True:
        return False
    if domain == "switch":
        return attributes.get("device_class") in _SAFE_SWITCH_DEVICE_CLASSES
    return True


def is_room_activity_eligible(state: Any, eligible_entity_ids: Iterable[str]) -> bool:
    """True for a candidate device that the room's stored policy approves."""
    entity_id = _state_value(state, "entity_id", "")
    return entity_id in frozenset(eligible_entity_ids) and is_room_activity_candidate(
        state
    )


def is_running_room_activity(state: Any, eligible_entity_ids: Iterable[str]) -> bool:
    """True for an eligible device that is on."""
    return (
        is_room_activity_eligible(state, eligible_entity_ids)
        and _state_value(state, "state") == "on"
    )


def summarize_openings(
    entity_states: Iterable[tuple[str, str | None]],
) -> dict[str, str | int]:
    """Count the open and unknown doors, windows and locks.

    A binary sensor is open when on and closed when off; a lock is open when
    unlocked and closed when locked. Any other state counts as unknown.
    """
    opened = 0
    unknown = 0
    for entity_id, state in entity_states:
        if entity_id.startswith("binary_sensor."):
            if state == "on":
                opened += 1
            elif state != "off":
                unknown += 1
        elif entity_id.startswith("lock."):
            if state == "unlocked":
                opened += 1
            elif state != "locked":
                unknown += 1
        else:
            unknown += 1
    return {
        "status": "open" if opened else ("unknown" if unknown else "all_closed"),
        "open_count": opened,
        "unknown_count": unknown,
    }


def room_activity_layout(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    """Lay out the room cards for the Now page.

    Up to four rooms are plain cards. With more, the first is featured, the
    next four are cards and the rest are counted for "view all".
    """
    count = len(rooms)
    if count == 0:
        return {"featured_room_id": None, "cards": [], "view_all_count": 0}
    if count <= 4:
        return {
            "featured_room_id": None,
            "cards": [room["room_id"] for room in rooms],
            "view_all_count": 0,
        }
    return {
        "featured_room_id": rooms[0]["room_id"],
        "cards": [room["room_id"] for room in rooms[1:5]],
        "view_all_count": count - 5,
    }


class NowDataEngine:
    """Persistent Now state, one key-value table per kind.

    An RLock keeps each read-modify-write whole.
    """

    def __init__(
        self,
        recents_table: Any,
        policies_table: Any,
        config_table: Any,
        restores_table: Any,
        idempotency_table: Any,
    ) -> None:
        self._recents = recents_table
        self._policies = policies_table
        self._config = config_table
        self._restores = restores_table
        self._idempotency = idempotency_table
        self._lock = threading.RLock()

    def record_successful_control(
        self, member_id: str, entity_id: str, at: datetime | None = None
    ) -> None:
        """Record a device the member controlled, newest first."""
        if not member_id or not isinstance(entity_id, str) or "." not in entity_id:
            return
        with self._lock:
            current = self._recents.get(member_id) or {}
            items = current.get("items", []) if isinstance(current, dict) else []
            cleaned = [
                item
                for item in items
                if isinstance(item, dict) and item.get("entity_id") != entity_id
            ]
            cleaned.insert(0, {"entity_id": entity_id, "at": _timestamp(at)})
            self._recents[member_id] = {"items": cleaned[:_MAX_RECENTS]}

    def recents_for(self, member_id: str) -> list[dict[str, str]]:
        """The member's recent controls, newest first."""
        record = self._recents.get(member_id) or {}
        items = record.get("items", []) if isinstance(record, dict) else []
        return [
            {"entity_id": item["entity_id"], "at": item["at"]}
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("entity_id"), str)
            and isinstance(item.get("at"), str)
        ]

    def set_room_policy(
        self, room_id: str, participates: Any, eligible_entity_ids: Any
    ) -> dict[str, Any]:
        """Store a room's activity policy and return it.

        now_api has already checked that every entity is a candidate in this
        room.
        """
        if not isinstance(participates, bool):
            raise NowDataError("participates must be a boolean")
        if not isinstance(eligible_entity_ids, list) or any(
            not isinstance(entity_id, str) or "." not in entity_id
            for entity_id in eligible_entity_ids
        ):
            raise NowDataError("eligible_entity_ids must be a list of entity ids")
        with self._lock:
            policy = {
                "participates": participates,
                "eligible_entity_ids": list(dict.fromkeys(eligible_entity_ids)),
            }
            self._policies[room_id] = policy
        return {"room_id": room_id, **policy}

    def room_policy(self, room_id: str) -> dict[str, Any]:
        """The room's policy; a room without a valid one does not take part."""
        record = self._policies.get(room_id)
        if not isinstance(record, dict):
            record = {}
        entity_ids = record.get("eligible_entity_ids")
        return {
            "participates": bool(record.get("participates") is True),
            "eligible_entity_ids": [
                entity_id for entity_id in entity_ids if isinstance(entity_id, str)
            ]
            if isinstance(entity_ids, list)
            else [],
        }

    def room_participates(self, room_id: str) -> bool:
        """True when the room takes part in room commands."""
        return self.room_policy(room_id)["participates"]

    def configure(self, payload: Any) -> dict[str, Any]:
        """Merge the fields present in the payload into the Now config.

        Checks shapes only; now_api checks that the entities and scenes exist
        and are of the right kind. Returns the whole config.
        """
        if not isinstance(payload, dict):
            raise NowDataError("Body must be a JSON object")
        allowed = {
            "outdoor_weather_entity_id",
            "air_quality_entity_id",
            "contact_entity_ids",
            "suggested_scene_id",
            "pinned_scene_ids",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise NowDataError(
                f"Unknown Now configuration field: {sorted(unknown)[0]!r}"
            )
        with self._lock:
            record = self._config.get("global") or {}
            if "outdoor_weather_entity_id" in payload:
                record["outdoor_weather_entity_id"] = _optional_entity_id(
                    payload["outdoor_weather_entity_id"], "outdoor_weather_entity_id"
                )
            if "air_quality_entity_id" in payload:
                record["air_quality_entity_id"] = _optional_entity_id(
                    payload["air_quality_entity_id"], "air_quality_entity_id"
                )
            if "suggested_scene_id" in payload:
                raw = payload["suggested_scene_id"]
                if raw is not None and (not isinstance(raw, str) or not raw):
                    raise NowDataError("suggested_scene_id must be a string or null")
                record["suggested_scene_id"] = raw
            if "contact_entity_ids" in payload:
                raw = payload["contact_entity_ids"]
                if not isinstance(raw, list) or len(raw) > _MAX_CONTACTS:
                    raise NowDataError(
                        f"contact_entity_ids must contain at most {_MAX_CONTACTS} entity ids"
                    )
                if any(
                    not isinstance(item, str)
                    or not item.startswith(("binary_sensor.", "lock."))
                    for item in raw
                ):
                    raise NowDataError(
                        "contact_entity_ids must be binary_sensor or lock entity ids"
                    )
                record["contact_entity_ids"] = list(dict.fromkeys(raw))
            if "pinned_scene_ids" in payload:
                raw = payload["pinned_scene_ids"]
                if not isinstance(raw, list) or len(raw) > _MAX_PINNED_MOMENTS:
                    raise NowDataError(
                        f"pinned_scene_ids must contain at most {_MAX_PINNED_MOMENTS} scene ids"
                    )
                if any(not isinstance(item, str) or not item for item in raw):
                    raise NowDataError("pinned_scene_ids must be scene id strings")
                record["pinned_scene_ids"] = list(dict.fromkeys(raw))
            self._config["global"] = record
        return self.config()

    def config(self) -> dict[str, Any]:
        """The Now config, with every field present."""
        raw = self._config.get("global") or {}
        return {
            "outdoor_weather_entity_id": raw.get("outdoor_weather_entity_id"),
            "air_quality_entity_id": raw.get("air_quality_entity_id"),
            "contact_entity_ids": list(raw.get("contact_entity_ids") or []),
            "suggested_scene_id": raw.get("suggested_scene_id"),
            "pinned_scene_ids": list(raw.get("pinned_scene_ids") or []),
        }

    @staticmethod
    def validate_idempotency_key(value: Any) -> str:
        """Return the client's idempotency key, or raise if it is malformed."""
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise NowDataError("idempotency_key must be 8-128 URL-safe characters")
        return value

    def idempotent_result(
        self, member_id: str, room_id: str, action: str, key: str
    ) -> dict[str, Any] | None:
        """The stored answer for this member's (room, action, key), if any."""
        record = self._idempotency.get(member_id) or {}
        result = (record.get("results") or {}).get(f"{room_id}:{action}:{key}")
        return dict(result) if isinstance(result, dict) else None

    def save_idempotent_result(
        self,
        member_id: str,
        room_id: str,
        action: str,
        key: str,
        result: dict[str, Any],
    ) -> None:
        """Store a room command's answer so a retry replays it."""
        with self._lock:
            record = self._idempotency.get(member_id) or {}
            results = (
                record.get("results") if isinstance(record.get("results"), dict) else {}
            )
            results[f"{room_id}:{action}:{key}"] = result
            # Keep the newest entries; JSON keeps insertion order.
            while len(results) > _MAX_IDEMPOTENCY_ENTRIES:
                results.pop(next(iter(results)))
            self._idempotency[member_id] = {"results": results}

    def restore_set(self, room_id: str) -> list[str]:
        """What the room's last off switched off and on has not yet restored."""
        record = self._restores.get(room_id) or {}
        entities = (record.get("entity_ids") or []) if isinstance(record, dict) else []
        return [item for item in entities if isinstance(item, str)]

    def save_restore_set(self, room_id: str, entity_ids: Iterable[str]) -> None:
        """Replace the room's restore set; an empty set removes it."""
        ids = list(dict.fromkeys(entity_ids))
        with self._lock:
            if ids:
                self._restores[room_id] = {
                    "entity_ids": ids,
                    "captured_at": _timestamp(),
                }
            else:
                self._restores.pop(room_id, None)

    def extend_restore_set(self, room_id: str, entity_ids: Iterable[str]) -> list[str]:
        """Add newly switched-off ids to the room's restore set and return it.

        Ids keep their first-captured order, each once.
        """
        with self._lock:
            self.save_restore_set(room_id, [*self.restore_set(room_id), *entity_ids])
            return self.restore_set(room_id)

    def consume_restore_set(self, room_id: str) -> list[str]:
        """Return the room's restore set and clear it."""
        with self._lock:
            ids = self.restore_set(room_id)
            self._restores.pop(room_id, None)
            return ids
