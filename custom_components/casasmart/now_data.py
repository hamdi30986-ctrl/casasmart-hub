"""Server-owned data and command state for the CasaSmart Now surface.

The client never derives this product state from Home Assistant history.  In
particular, recent controls are recorded only after a CasaSmart command has
succeeded, and room restore sets are written by the Hub, not reconstructed by
the client after a restart.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import threading
from typing import Any, Iterable


_MAX_RECENTS = 48
_MAX_PINNED_MOMENTS = 12
_MAX_CONTACTS = 64
_MAX_IDEMPOTENCY_ENTRIES = 64
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_ROOM_ACTIVITY_DOMAINS = frozenset({"light", "fan", "switch"})
# Home Assistant permits a switch to have no device class.  That generic value
# is not a safety classification, so it must never be admitted to a room bulk
# operation.  The Hub policy must additionally name every participating entity.
_SAFE_SWITCH_DEVICE_CLASSES = frozenset({"switch"})
class NowDataError(Exception):
    """Raised when persisted Now configuration is invalid."""


def _optional_entity_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "." not in value or len(value) > 255:
        raise NowDataError(f"{field} must be an entity_id string or null")
    return value


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _state_value(state: Any, field: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(field, default)
    return getattr(state, field, default)


def _state_attributes(state: Any) -> dict[str, Any]:
    attributes = _state_value(state, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


def is_room_activity_candidate(state: Any) -> bool:
    """Return whether an entity can be added to the explicit Hub allowlist."""

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
    """Return whether an entity is both a safe candidate and Hub-authorized.

    A client cannot make an entity eligible by sending it in a command.  The
    entity must appear in the persisted per-room policy selected by an admin.
    """

    entity_id = _state_value(state, "entity_id", "")
    return entity_id in frozenset(eligible_entity_ids) and is_room_activity_candidate(state)


def is_running_room_activity(state: Any, eligible_entity_ids: Iterable[str]) -> bool:
    """A running room device is an eligible device whose state is ``on``."""

    return is_room_activity_eligible(state, eligible_entity_ids) and _state_value(state, "state") == "on"


def summarize_openings(
    entity_states: Iterable[tuple[str, str | None]],
) -> dict[str, str | int]:
    """Aggregate door/window sensors and locks without inventing closure.

    Binary sensors use Home Assistant's contact convention (on=open,
    off=closed). Locks are open only when unlocked and closed only when locked.
    Every transient, error, unavailable, or unknown value remains unknown.
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
    """Apply the fixed 0--4 / 5 / 6+ Now layout contract."""

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
        "view_all_count": max(0, count - 5),
    }


class NowDataEngine:
    """Small persistent store for Now-only user and room state."""

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
        """Record a successful user-originated command, newest first."""

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
        record = self._policies.get(room_id) or {}
        entity_ids = record.get("eligible_entity_ids") if isinstance(record, dict) else []
        return {
            "participates": bool(record.get("participates") is True),
            "eligible_entity_ids": [
                entity_id for entity_id in entity_ids if isinstance(entity_id, str)
            ] if isinstance(entity_ids, list) else [],
        }

    def room_participates(self, room_id: str) -> bool:
        return self.room_policy(room_id)["participates"]

    def configure(self, payload: Any) -> dict[str, Any]:
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
            raise NowDataError(f"Unknown Now configuration field: {sorted(unknown)[0]!r}")
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
                    raise NowDataError(f"contact_entity_ids must contain at most {_MAX_CONTACTS} entity ids")
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
                    raise NowDataError(f"pinned_scene_ids must contain at most {_MAX_PINNED_MOMENTS} scene ids")
                if any(not isinstance(item, str) or not item for item in raw):
                    raise NowDataError("pinned_scene_ids must be scene id strings")
                record["pinned_scene_ids"] = list(dict.fromkeys(raw))
            self._config["global"] = record
        return self.config()

    def config(self) -> dict[str, Any]:
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
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise NowDataError("idempotency_key must be 8-128 URL-safe characters")
        return value

    def idempotent_result(
        self, member_id: str, room_id: str, action: str, key: str
    ) -> dict[str, Any] | None:
        record = self._idempotency.get(member_id) or {}
        result = (record.get("results") or {}).get(f"{room_id}:{action}:{key}")
        return dict(result) if isinstance(result, dict) else None

    def save_idempotent_result(
        self, member_id: str, room_id: str, action: str, key: str, result: dict[str, Any]
    ) -> None:
        with self._lock:
            record = self._idempotency.get(member_id) or {}
            results = record.get("results") if isinstance(record.get("results"), dict) else {}
            results[f"{room_id}:{action}:{key}"] = result
            # Deterministic bounded persistence; insertion order is preserved by JSON.
            while len(results) > _MAX_IDEMPOTENCY_ENTRIES:
                results.pop(next(iter(results)))
            self._idempotency[member_id] = {"results": results}

    def restore_set(self, room_id: str) -> list[str]:
        record = self._restores.get(room_id) or {}
        entities = (record.get("entity_ids") or []) if isinstance(record, dict) else []
        return [item for item in entities if isinstance(item, str)]

    def save_restore_set(self, room_id: str, entity_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(entity_ids))
        with self._lock:
            if ids:
                self._restores[room_id] = {"entity_ids": ids, "captured_at": _timestamp()}
            else:
                self._restores.pop(room_id, None)

    def consume_restore_set(self, room_id: str) -> list[str]:
        with self._lock:
            ids = self.restore_set(room_id)
            self._restores.pop(room_id, None)
            return ids
