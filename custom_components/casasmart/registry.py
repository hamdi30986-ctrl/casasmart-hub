"""Registry engine: the hub's floors, rooms, room tags, devices and scenes.

Also stores per-member favorites. No Home Assistant imports; storage-touching
methods are synchronous (call via executor).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from typing import Any

try:
    from .entity_bridge import CommandError, validate_command
except ImportError:
    from entity_bridge import CommandError, validate_command

_LOGGER = logging.getLogger(__name__)


UNSET = object()

_NAME_MAX = 64
_ICON_MAX = 64
_MAX_FAVORITES = 200
_MAX_SCENE_ENTITIES = 50
_MAX_DEVICE_ENTITIES = 100
_MAX_ROOM_TAGS = 64
_MAX_TAG_ROOMS = 128
_TAG_COLORS = frozenset(
    {
        # Bright tablet palette. Keep legacy presets valid for existing clients
        # and stored tags; changing the UI must never recolor existing data.
        "#FFD45C",  # yellow
        "#85E0A3",  # green
        "#F5F3ED",  # white
        "#FF7777",  # red
        "#2563EB",  # blue
        "#EA580C",  # orange
        "#7C3AED",  # purple
        "#0F766E",  # teal
        "#475569",  # slate
        "#A16207",  # amber/brown
    }
)


class RegistryError(Exception):
    """Registry input rejected (maps to HTTP 400)."""


class UnknownItemError(RegistryError):
    """No floor/room/scene/assignment under that id (maps to HTTP 404)."""


class InUseError(RegistryError):
    """Deletion refused because something still references the item."""


class RoomMoveConflict(InUseError):
    """The reviewed room assignments no longer match the hub."""


class RoomMoveDenied(RegistryError):
    """A room move would cross the caller's authorized room scope."""


def _clean_name(name: Any, what: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RegistryError(f"{what} name is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise RegistryError(f"{what} name is too long (max {_NAME_MAX})")
    return cleaned


def _lenient_name(name: Any, fallback: str) -> str:
    """Import-only name cleaning: HA area/floor names are free user text
    we don't control — truncate instead of rejecting, never raise. A
    rejection here would abort integration setup (and keep aborting it
    on every restart) over a name the user typed into HA years ago."""
    if not isinstance(name, str) or not name.strip():
        return fallback
    return name.strip()[:_NAME_MAX]


def _lenient_sort_order(sort_order: Any) -> int:
    """Import-only: anything that isn't a plain int becomes 0."""
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        return 0
    return sort_order


def _clean_icon(icon: Any) -> str | None:
    if icon is None:
        return None
    if not isinstance(icon, str) or len(icon) > _ICON_MAX:
        raise RegistryError(f"icon must be a string of at most {_ICON_MAX} chars")
    return icon or None


def _clean_sort_order(sort_order: Any) -> int:
    if sort_order is None:
        return 0
    # bool is an int subclass — reject it explicitly.
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise RegistryError("sort_order must be an integer")
    return sort_order


def _clean_tag_color(color: Any) -> str:
    if not isinstance(color, str) or color.upper() not in _TAG_COLORS:
        raise RegistryError("tag color is not an allowed preset")
    return color.upper()


def _clean_favorite(favorite: Any) -> bool:
    """House-wide scene-favorite flag. Must be a real bool when present."""
    if not isinstance(favorite, bool):
        raise RegistryError("favorite must be a boolean")
    return favorite


def _clean_energy_flag(value: Any) -> bool:
    """Whether a scene may execute while Energy Saving is active."""
    if not isinstance(value, bool):
        raise RegistryError("works_during_energy_saving must be a boolean")
    return value


def _clean_entity_ids(
    value: Any, what: str = "entity_ids", max_count: int = _MAX_DEVICE_ENTITIES
) -> list[str]:
    """A list of entity_id strings — deduped, order preserved, capped."""
    if not isinstance(value, list) or any(
        not isinstance(eid, str) or "." not in eid for eid in value
    ):
        raise RegistryError(f"{what} must be a list of entity_id strings")
    if len(value) > max_count:
        raise RegistryError(f"At most {max_count} {what}")
    return list(dict.fromkeys(value))  # preserve order, drop dupes


def _clean_gang_map(value: Any, what: str) -> dict[str, str]:
    """A {gang-suffix -> value} map (gang_types / gang_names); None -> {}."""
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise RegistryError(f"{what} must be a map of strings to strings")
    return dict(value)


_VALID_GANG_PRESENTATIONS = frozenset({"grouped", "solo", "hidden"})


_KNOWN_GANG_TYPES = frozenset({"switch", "light", "fan", "heater", "outlet"})


def _clean_gang_type(value: Any) -> str:
    """Validate a gang's presentation type against the known set."""
    if not isinstance(value, str) or value not in _KNOWN_GANG_TYPES:
        raise RegistryError(
            "gang type must be one of: " + ", ".join(sorted(_KNOWN_GANG_TYPES))
        )
    return value


def _clean_gangs(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError("gangs must be a map")
    out: dict[str, dict[str, Any]] = {}
    for key, gang in value.items():
        if not isinstance(key, str) or not isinstance(gang, dict):
            raise RegistryError("gangs entries must be {entity_id: {...}}")
        presentation = gang.get("presentation", "grouped")
        if (
            not isinstance(presentation, str)
            or presentation not in _VALID_GANG_PRESENTATIONS
        ):
            raise RegistryError("gang presentation must be grouped, solo or hidden")
        gtype = gang.get("type")
        clean_type = "switch" if gtype is None else _clean_gang_type(gtype)
        icon = gang.get("icon")
        if icon is not None and (not isinstance(icon, str) or len(icon) > 64):
            raise RegistryError("gang icon must be a string of at most 64 chars")
        name = gang.get("name")
        if name is not None and not isinstance(name, str):
            raise RegistryError("gang name must be a string")
        out[key] = {
            "type": clean_type,
            "icon": icon,
            "name": name,
            "presentation": presentation,
            "room_id": _clean_optional_room(gang.get("room_id")),
            **({"room_override": True} if gang.get("room_override") is True else {}),
        }
    return out


def _gangs_backed_by(
    gangs: dict[str, dict[str, Any]], entity_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Keep only gangs whose control entity_id is a grabbed relay — a gang must
    map to a real entity in the record, never a phantom (so the gangs= write
    path can't invent one)."""
    allowed = set(entity_ids)
    return {key: gang for key, gang in gangs.items() if key in allowed}


def _clean_optional_room(value: Any) -> str | None:
    """A nullable device-level room id."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RegistryError("room_id must be a non-empty string or null")
    return value


def _clean_optional_name(name: Any) -> str | None:
    """A nullable display name — None passes through, else validated."""
    return None if name is None else _clean_name(name, "Device")


def _clean_device_type(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _NAME_MAX:
        raise RegistryError("device_type must be a string")
    return value


def _clean_scene_entities(entities: Any) -> list[dict[str, Any]]:
    """Validate a scene's command list against the entity-bridge whitelist."""
    if not isinstance(entities, list) or not entities:
        raise RegistryError("entities must be a non-empty list")
    if len(entities) > _MAX_SCENE_ENTITIES:
        raise RegistryError(f"A scene may hold at most {_MAX_SCENE_ENTITIES} entities")
    cleaned: list[dict[str, Any]] = []
    for item in entities:
        if not isinstance(item, dict):
            raise RegistryError("Each scene entity must be an object")
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise RegistryError("Each scene entity needs an entity_id")
        try:
            validate_command(entity_id, item.get("action"), item.get("data"))
        except CommandError as err:
            raise RegistryError(f"{entity_id}: {err}") from err
        try:
            json.dumps(item.get("data") or {}, allow_nan=False)
        except (TypeError, ValueError) as err:
            raise RegistryError(f"{entity_id}: data must be plain JSON") from err
        cleaned.append(
            {
                "entity_id": entity_id,
                "action": item["action"],
                "data": item.get("data") or {},
            }
        )
    return cleaned


class RegistryEngine:
    def __init__(
        self,
        floors_table: Any,
        rooms_table: Any,
        devices_table: Any,
        scenes_table: Any,
        favorites_table: Any,
        user_devices_table: Any,
        room_tags_table: Any,
    ) -> None:
        self._floors = floors_table
        self._rooms = rooms_table
        self._devices = devices_table
        self._scenes = scenes_table
        self._favorites = favorites_table
        self._user_devices = user_devices_table
        self._room_tags = room_tags_table

        self._lock = threading.RLock()

        self._mirror_lock = threading.Lock()

        self._assignment_cache: dict[str, tuple[str | None, str | None]] = {}

        self._room_names: dict[str, str] = {}

    def warm_up(self) -> None:
        """Load the event-loop mirrors from storage (executor, at setup)."""
        with self._lock:
            assignments = {
                entity_id: (record.get("room_id"), record.get("display_name"))
                for entity_id, record in self._devices.items()
            }
            room_names = {
                room_id: record.get("name", room_id)
                for room_id, record in self._rooms.items()
            }
        with self._mirror_lock:
            self._assignment_cache = assignments
            self._room_names = room_names

    def _mirror_assignment(self, entity_id: str, record: dict[str, Any]) -> None:
        with self._mirror_lock:
            self._assignment_cache[entity_id] = (
                record.get("room_id"),
                record.get("display_name"),
            )

    def room_of(self, entity_id: str) -> Any:
        """The entity's registry room: room_id, None (explicit Unassigned),
        or the UNSET sentinel when no record exists (fall back to HA)."""
        with self._mirror_lock:
            cached = self._assignment_cache.get(entity_id)
        return UNSET if cached is None else cached[0]

    def display_name_of(self, entity_id: str) -> str | None:
        """The installer-set display name, or None (use HA friendly name)."""
        with self._mirror_lock:
            cached = self._assignment_cache.get(entity_id)
        return None if cached is None else cached[1]

    def room_name(self, room_id: str) -> str | None:
        """A room's display name, or None for an unknown room."""
        with self._mirror_lock:
            return self._room_names.get(room_id)

    def list_floors(self) -> list[dict[str, Any]]:
        return [
            {"floor_id": floor_id, **record}
            for floor_id, record in self._floors.items()
        ]

    def create_floor(self, name: Any, sort_order: Any = None) -> dict[str, Any]:
        record = {
            "name": _clean_name(name, "Floor"),
            "sort_order": _clean_sort_order(sort_order),
        }
        with self._lock:
            floor_id = f"floor-{secrets.token_urlsafe(8)}"
            self._floors[floor_id] = record
        _LOGGER.info("Registry: floor %s created (%s)", floor_id, record["name"])
        return {"floor_id": floor_id, **record}

    def update_floor(
        self, floor_id: str, name: Any = ..., sort_order: Any = ...
    ) -> dict[str, Any]:
        """Edit a floor. ``...`` sentinels mean "leave unchanged" — an
        explicit null is validated (and rejected) like any other value."""
        with self._lock:
            record = self._floors.get(floor_id)
            if record is None:
                raise UnknownItemError("Unknown floor")
            if name is not ...:
                record["name"] = _clean_name(name, "Floor")
            if sort_order is not ...:
                record["sort_order"] = _clean_sort_order(sort_order)
            self._floors[floor_id] = record  # persist
        return {"floor_id": floor_id, **record}

    def delete_floor(self, floor_id: str) -> None:
        """Refuse while rooms still reference the floor — explicit beats
        a silent cascade for something the installer did by hand."""
        with self._lock:
            if floor_id not in self._floors:
                raise UnknownItemError("Unknown floor")
            in_use = [
                room_id
                for room_id, room in self._rooms.items()
                if room.get("floor_id") == floor_id
            ]
            if in_use:
                raise InUseError(
                    f"Floor still has {len(in_use)} room(s) — move them first"
                )
            del self._floors[floor_id]
        _LOGGER.info("Registry: floor %s deleted", floor_id)

    def list_rooms(self) -> list[dict[str, Any]]:
        return [
            {"room_id": room_id, **record} for room_id, record in self._rooms.items()
        ]

    def create_room(
        self,
        name: Any,
        floor_id: Any = None,
        icon: Any = None,
        sort_order: Any = None,
    ) -> dict[str, Any]:
        record = {
            "name": _clean_name(name, "Room"),
            "floor_id": self._checked_floor_id(floor_id),
            "icon": _clean_icon(icon),
            "sort_order": _clean_sort_order(sort_order),
        }
        with self._lock:
            room_id = f"room-{secrets.token_urlsafe(8)}"
            self._rooms[room_id] = record
        with self._mirror_lock:
            self._room_names[room_id] = record["name"]
        _LOGGER.info("Registry: room %s created (%s)", room_id, record["name"])
        return {"room_id": room_id, **record}

    def update_room(
        self,
        room_id: str,
        name: Any = ...,
        floor_id: Any = ...,
        icon: Any = ...,
        sort_order: Any = ...,
    ) -> dict[str, Any]:
        """Edit a room. ``...`` sentinels mean "leave unchanged" — for
        the nullable fields an explicit None clears them."""
        with self._lock:
            record = self._rooms.get(room_id)
            if record is None:
                raise UnknownItemError("Unknown room")
            if name is not ...:
                record["name"] = _clean_name(name, "Room")
            if floor_id is not ...:
                record["floor_id"] = self._checked_floor_id(floor_id)
            if icon is not ...:
                record["icon"] = _clean_icon(icon)
            if sort_order is not ...:
                record["sort_order"] = _clean_sort_order(sort_order)
            self._rooms[room_id] = record  # persist
        with self._mirror_lock:
            self._room_names[room_id] = record["name"]
        return {"room_id": room_id, **record}

    def delete_room(self, room_id: str) -> int:
        with self._lock:
            if room_id not in self._rooms:
                raise UnknownItemError("Unknown room")
            cleared = 0
            for entity_id, record in list(self._devices.items()):
                if record.get("room_id") == room_id:
                    record["room_id"] = None
                    self._devices[entity_id] = record

                    self._mirror_assignment(entity_id, record)
                    cleared += 1
            # Solo cards carry their room on the gang, not the entity
            # assignment. Keep them explicitly Unassigned when that room is
            # removed; leaving a dangling id hides them from every room.
            for device_id, device in list(self._user_devices.items()):
                gangs = device.get("gangs", {})
                if any(gang.get("room_id") == room_id for gang in gangs.values()):
                    self._user_devices[device_id] = {
                        **device,
                        "gangs": {
                            key: (
                                {**gang, "room_id": None, "room_override": True}
                                if gang.get("room_id") == room_id
                                else gang
                            )
                            for key, gang in gangs.items()
                        },
                    }
            tags = self._room_tags_doc()
            tags_changed = False
            for tag_id, tag in list(tags.items()):
                assigned = tag.get("room_ids", [])
                if room_id not in assigned:
                    continue
                remaining = [
                    candidate for candidate in assigned if candidate != room_id
                ]
                if remaining:
                    tag["room_ids"] = remaining
                else:
                    # A tag cannot be created without a room, so deleting its
                    # last room removes the now-unusable tag as well.
                    del tags[tag_id]
                tags_changed = True
            if tags_changed:
                self._room_tags["all"] = tags
            del self._rooms[room_id]
        with self._mirror_lock:
            self._room_names.pop(room_id, None)
        _LOGGER.info(
            "Registry: room %s deleted (%d device(s) unassigned)", room_id, cleared
        )
        return cleared

    # Room tags are stored as one small document so a multi-room edit is one
    # SQLite write. That prevents an interrupted update from leaving different
    # rooms with half of the requested tag assignment.
    def _room_tags_doc(self) -> dict[str, dict[str, Any]]:
        raw = self._room_tags.get("all")
        if not isinstance(raw, dict):
            return {}
        return {
            str(tag_id): dict(record)
            for tag_id, record in raw.items()
            if isinstance(tag_id, str) and isinstance(record, dict)
        }

    def list_room_tags(self) -> list[dict[str, Any]]:
        with self._lock:
            known_rooms = set(self._rooms)
            tags = self._room_tags_doc()
            result: list[dict[str, Any]] = []
            for tag_id, record in tags.items():
                name = record.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                color = record.get("color")
                if not isinstance(color, str) or color.upper() not in _TAG_COLORS:
                    color = "#475569"
                raw_room_ids = record.get("room_ids")
                room_ids = raw_room_ids if isinstance(raw_room_ids, list) else []
                result.append(
                    {
                        "tag_id": tag_id,
                        "name": name.strip(),
                        "color": color.upper(),
                        "room_ids": list(
                            dict.fromkeys(
                                room_id
                                for room_id in room_ids
                                if isinstance(room_id, str) and room_id in known_rooms
                            )
                        ),
                    }
                )
            return result

    def _clean_tag_room_ids(self, room_ids: Any) -> list[str]:
        if not isinstance(room_ids, list) or any(
            not isinstance(room_id, str) for room_id in room_ids
        ):
            raise RegistryError("room_ids must be a list of room ids")
        unique = list(dict.fromkeys(room_ids))
        if not unique:
            raise RegistryError("Select at least one room")
        if len(unique) > _MAX_TAG_ROOMS:
            raise RegistryError(f"A tag may include at most {_MAX_TAG_ROOMS} rooms")
        unknown = [room_id for room_id in unique if room_id not in self._rooms]
        if unknown:
            raise RegistryError("Unknown room_id")
        return unique

    def _assign_tag_rooms(
        self,
        tags: dict[str, dict[str, Any]],
        tag_id: str,
        room_ids: list[str],
    ) -> None:
        selected = set(room_ids)
        for other_id, record in tags.items():
            if other_id == tag_id:
                continue
            record["room_ids"] = [
                room_id
                for room_id in record.get("room_ids", [])
                if room_id not in selected
            ]

    def create_room_tag(self, name: Any, color: Any, room_ids: Any) -> dict[str, Any]:
        with self._lock:
            tags = self._room_tags_doc()
            if len(tags) >= _MAX_ROOM_TAGS:
                raise RegistryError(f"At most {_MAX_ROOM_TAGS} room tags")
            clean_name = _clean_name(name, "Tag")
            if any(
                str(record.get("name", "")).casefold() == clean_name.casefold()
                for record in tags.values()
            ):
                raise RegistryError("Tag name already exists")
            clean_rooms = self._clean_tag_room_ids(room_ids)
            tag_id = f"tag-{secrets.token_urlsafe(8)}"
            record = {
                "name": clean_name,
                "color": _clean_tag_color(color),
                "room_ids": clean_rooms,
            }
            self._assign_tag_rooms(tags, tag_id, clean_rooms)
            tags[tag_id] = record
            self._room_tags["all"] = tags
        return {"tag_id": tag_id, **record}

    def update_room_tag(
        self,
        tag_id: str,
        name: Any = ...,
        color: Any = ...,
        room_ids: Any = ...,
    ) -> dict[str, Any]:
        with self._lock:
            tags = self._room_tags_doc()
            record = tags.get(tag_id)
            if record is None:
                raise UnknownItemError("Unknown room tag")
            if name is not ...:
                clean_name = _clean_name(name, "Tag")
                if any(
                    other_id != tag_id
                    and str(other.get("name", "")).casefold() == clean_name.casefold()
                    for other_id, other in tags.items()
                ):
                    raise RegistryError("Tag name already exists")
                record["name"] = clean_name
            if color is not ...:
                record["color"] = _clean_tag_color(color)
            if room_ids is not ...:
                clean_rooms = self._clean_tag_room_ids(room_ids)
                self._assign_tag_rooms(tags, tag_id, clean_rooms)
                record["room_ids"] = clean_rooms
            tags[tag_id] = record
            self._room_tags["all"] = tags
        return {"tag_id": tag_id, **record}

    def delete_room_tag(self, tag_id: str) -> None:
        with self._lock:
            tags = self._room_tags_doc()
            if tag_id not in tags:
                raise UnknownItemError("Unknown room tag")
            del tags[tag_id]
            self._room_tags["all"] = tags

    def _checked_floor_id(self, floor_id: Any) -> str | None:
        if floor_id is None:
            return None
        if not isinstance(floor_id, str) or floor_id not in self._floors:
            raise RegistryError("Unknown floor_id")
        return floor_id

    def list_assignments(self) -> dict[str, dict[str, Any]]:
        """entity_id -> {room_id, display_name, sort_order}."""
        return dict(self._devices.items())

    def assign_device(
        self,
        entity_id: str,
        room_id: Any = ...,
        display_name: Any = ...,
        sort_order: Any = ...,
    ) -> dict[str, Any]:
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise RegistryError("entity_id is required")
        with self._lock:
            record = self._devices.get(entity_id) or {
                "room_id": None,
                "display_name": None,
                "sort_order": 0,
            }
            if room_id is not ...:
                if room_id is not None and (
                    not isinstance(room_id, str) or room_id not in self._rooms
                ):
                    raise RegistryError("Unknown room_id")
                record["room_id"] = room_id
            if display_name is not ...:
                if display_name is not None and not isinstance(display_name, str):
                    raise RegistryError("display_name must be a string or null")
                if display_name is not None:
                    display_name = (
                        _clean_name(display_name, "Device")
                        if display_name.strip()
                        else None
                    )
                record["display_name"] = display_name
            if sort_order is not ...:
                record["sort_order"] = _clean_sort_order(sort_order)
            self._devices[entity_id] = record
            self._mirror_assignment(entity_id, record)
        return {"entity_id": entity_id, **record}

    def move_device_room(
        self,
        storage,
        actor: str,
        payload: dict[str, Any],
        *,
        assignable_ids: set[str],
        fallback_rooms: dict[str, str | None],
        scope: list[str] | None = None,
    ) -> dict[str, Any]:
        """Move one saved device (or solo gang) in one durable transaction.

        expected_rooms is the client's reviewed entity membership AND source
        room map. Compare it inside the registry lock, not against an earlier
        snapshot. The idempotency receipt is committed with the assignments.
        No HA control or entity-registry mutation is performed here.
        """
        allowed = {
            "ha_device_id",
            "gang_entity_id",
            "room_id",
            "expected_rooms",
            "expected_gang_override",
            "idempotency_key",
        }
        if set(payload) - allowed or "room_id" not in payload:
            raise RegistryError("Invalid room move fields")
        device_id = payload.get("ha_device_id")
        gang_id = payload.get("gang_entity_id")
        key = payload.get("idempotency_key")
        room_id = payload.get("room_id")
        expected = payload.get("expected_rooms")
        expected_override = payload.get("expected_gang_override")
        if not isinstance(actor, str) or not actor:
            raise RoomMoveDenied("Missing caller identity")
        if not isinstance(device_id, str) or not 0 < len(device_id) <= 255:
            raise RegistryError("ha_device_id is required")
        if gang_id is not None and (not isinstance(gang_id, str) or len(gang_id) > 255):
            raise RegistryError("Invalid gang_entity_id")
        if gang_id is not None and not isinstance(expected_override, bool):
            raise RegistryError("expected_gang_override is required for a solo gang")
        if gang_id is None and expected_override is not None:
            raise RegistryError("expected_gang_override requires a solo gang")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise RegistryError("Invalid idempotency_key")
        if room_id is not None and (not isinstance(room_id, str) or not room_id):
            raise RegistryError("Invalid room_id")
        if (
            not isinstance(expected, dict)
            or not 0 < len(expected) <= _MAX_DEVICE_ENTITIES
        ):
            raise RegistryError("expected_rooms must contain 1-100 primary entities")
        if any(
            not isinstance(k, str)
            or len(k) > 255
            or "." not in k
            or (v is not None and not isinstance(v, str))
            for k, v in expected.items()
        ):
            raise RegistryError("Invalid expected_rooms")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt_key = hashlib.sha256(f"{actor}:{key}".encode()).hexdigest()
        receipts = storage.table("registry_room_moves")
        now = time.time()
        with self._lock:
            record = self._user_devices.get(device_id)
            if record is None:
                raise UnknownItemError("Device is no longer imported")
            primary_entities = list(
                dict.fromkeys(
                    eid
                    for eid in record.get("entity_ids", [])
                    if eid not in record.get("config_entity_ids", [])
                )
            )
            entities = primary_entities
            gang = None
            if gang_id is not None:
                gang = record.get("gangs", {}).get(gang_id)
                if (
                    gang_id not in entities
                    or not gang
                    or gang.get("presentation") != "solo"
                ):
                    raise RoomMoveConflict(
                        "Device presentation changed; refresh and retry"
                    )
                entities = [gang_id]
            if not entities or set(entities) != set(expected):
                raise RoomMoveConflict("Device membership changed; refresh and retry")
            if any(eid not in assignable_ids for eid in entities):
                raise UnknownItemError("A primary device entity is no longer available")
            if room_id is not None and room_id not in self._rooms:
                raise UnknownItemError("Destination room no longer exists")
            current = {}
            for eid in entities:
                assignment = self._devices.get(eid)
                current[eid] = (
                    assignment.get("room_id")
                    if assignment is not None
                    else fallback_rooms.get(eid)
                    if fallback_rooms.get(eid) in self._rooms
                    else None
                )
            permission_rooms = dict(current)
            if gang is not None:
                if gang.get("room_id") is not None or gang.get("room_override") is True:
                    current[gang_id] = gang.get("room_id")
                    permission_rooms[gang_id] = gang.get("room_id")
                # The acknowledgment contains the owning record, so require
                # access to its other primary members too (as registry GET
                # does). Never leak another room's gang metadata in the echo.
                for eid in primary_entities:
                    other = record.get("gangs", {}).get(eid, {})
                    assignment = self._devices.get(eid)
                    inherited = (
                        assignment.get("room_id")
                        if assignment is not None
                        else fallback_rooms.get(eid)
                    )
                    permission_rooms[eid] = (
                        other.get("room_id")
                        if other.get("room_override") is True
                        or other.get("room_id") is not None
                        else inherited
                    )
            if scope is not None and (
                room_id not in scope
                or any(value not in scope for value in permission_rooms.values())
            ):
                raise RoomMoveDenied("Room move is outside your allowed rooms")
            receipt = receipts.get(receipt_key)
            if receipt and receipt.get("expires_at", 0) > now:
                if receipt["fingerprint"] != fingerprint:
                    raise RoomMoveConflict("Idempotency key was used for another move")
                # Do not replay a stale receipt over a later, unrelated move.
                return {**receipt["result"], "replayed": True}
            if current != expected:
                raise RoomMoveConflict("Room assignment changed; refresh and retry")
            if (
                gang is not None
                and (gang.get("room_override") is True) != expected_override
            ):
                raise RoomMoveConflict(
                    "Gang room inheritance changed; refresh and retry"
                )
            assignments = []
            updated_record = None
            with storage.transaction():
                if gang is not None:
                    updated_record = {
                        **record,
                        "gangs": {
                            **record.get("gangs", {}),
                            gang_id: {
                                **gang,
                                "room_id": room_id,
                                "room_override": True,
                            },
                        },
                    }
                    self._user_devices[device_id] = updated_record
                else:
                    for eid in entities:
                        assignment = {
                            **(
                                self._devices.get(eid)
                                or {
                                    "display_name": None,
                                    "sort_order": 0,
                                }
                            ),
                            "room_id": room_id,
                        }
                        self._devices[eid] = assignment
                        assignments.append({"entity_id": eid, **assignment})
                result = {
                    "ha_device_id": device_id,
                    "gang_entity_id": gang_id,
                    "room_id": room_id,
                    "assignments": assignments,
                    "user_device": (
                        self._serve_user_device(device_id, updated_record)
                        if updated_record is not None
                        else None
                    ),
                    "replayed": False,
                }
                # Bounded receipts. Pruning and receipt creation share the
                # transaction, so disk failure cannot produce a false receipt.
                entries = sorted(
                    receipts.items(), key=lambda item: item[1].get("expires_at", 0)
                )
                for old_key, value in entries:
                    if value.get("expires_at", 0) <= now:
                        del receipts[old_key]
                while len(receipts) >= 1024:
                    old_key = min(
                        receipts.items(), key=lambda item: item[1]["expires_at"]
                    )[0]
                    del receipts[old_key]
                receipts[receipt_key] = {
                    "fingerprint": fingerprint,
                    "expires_at": now + 86400,
                    "result": result,
                }
            # Publish the in-memory room mirror only after SQLite commits.
            with self._mirror_lock:
                self._assignment_cache.update(
                    {
                        item["entity_id"]: (
                            item.get("room_id"),
                            item.get("display_name"),
                        )
                        for item in assignments
                    }
                )
            return result

    def remove_assignment(self, entity_id: str) -> None:
        """Drop the record entirely — the entity reverts to the HA-area
        fallback (vs ``room_id=None`` which pins it to Unassigned)."""
        with self._lock:
            try:
                del self._devices[entity_id]
            except KeyError:
                raise UnknownItemError("No assignment for that entity") from None
        with self._mirror_lock:
            self._assignment_cache.pop(entity_id, None)

    @staticmethod
    def _scene_out(scene_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Public scene shape. ``favorite`` defaults False for legacy
        records that predate the house-wide favorites flag."""
        return {
            "scene_id": scene_id,
            **record,
            "favorite": bool(record.get("favorite", False)),
            "works_during_energy_saving": (
                record.get("works_during_energy_saving") is True
            ),
        }

    def list_scenes(self) -> list[dict[str, Any]]:
        return [
            self._scene_out(scene_id, record)
            for scene_id, record in self._scenes.items()
        ]

    def get_scene(self, scene_id: str) -> dict[str, Any]:
        record = self._scenes.get(scene_id)
        if record is None:
            raise UnknownItemError("Unknown scene")
        return self._scene_out(scene_id, record)

    def create_scene(
        self,
        name: Any,
        entities: Any,
        icon: Any = None,
        works_during_energy_saving: Any = False,
    ) -> dict[str, Any]:
        record = {
            "name": _clean_name(name, "Scene"),
            "icon": _clean_icon(icon),
            "entities": _clean_scene_entities(entities),
            "favorite": False,
            "works_during_energy_saving": _clean_energy_flag(
                works_during_energy_saving
            ),
        }
        with self._lock:
            scene_id = f"scene-{secrets.token_urlsafe(8)}"
            self._scenes[scene_id] = record
        _LOGGER.info("Registry: scene %s created (%s)", scene_id, record["name"])
        return self._scene_out(scene_id, record)

    def update_scene(
        self,
        scene_id: str,
        name: Any = ...,
        entities: Any = ...,
        icon: Any = ...,
        favorite: Any = ...,
        works_during_energy_saving: Any = ...,
    ) -> dict[str, Any]:
        """Edit a scene. ``...`` sentinels mean "leave unchanged"."""
        with self._lock:
            record = self._scenes.get(scene_id)
            if record is None:
                raise UnknownItemError("Unknown scene")
            if name is not ...:
                record["name"] = _clean_name(name, "Scene")
            if entities is not ...:
                record["entities"] = _clean_scene_entities(entities)
            if icon is not ...:
                record["icon"] = _clean_icon(icon)
            if favorite is not ...:
                record["favorite"] = _clean_favorite(favorite)
            if works_during_energy_saving is not ...:
                record["works_during_energy_saving"] = _clean_energy_flag(
                    works_during_energy_saving
                )
            self._scenes[scene_id] = record  # persist
        return self._scene_out(scene_id, record)

    def delete_scene(self, scene_id: str) -> None:
        with self._lock:
            try:
                del self._scenes[scene_id]
            except KeyError:
                raise UnknownItemError("Unknown scene") from None
        _LOGGER.info("Registry: scene %s deleted", scene_id)

    def get_favorites(self, member_id: str) -> list[str]:
        record = self._favorites.get(member_id)
        if record is None:
            return []
        return list(record.get("entity_ids", []))

    def set_favorites(self, member_id: str, entity_ids: Any) -> list[str]:
        """Replace a member's favorites list (order is meaningful). Keyed by
        member_id so a person's devices share one list; the unpair path prunes
        it when the member's last device leaves (see delete_favorites)."""
        deduped = _clean_entity_ids(entity_ids, "favorites", _MAX_FAVORITES)
        with self._lock:
            self._favorites[member_id] = {"entity_ids": deduped}
        return deduped

    def delete_favorites(self, member_id: str) -> None:
        """Drop a member's favorites row — called when their last device is
        unpaired so the row can't orphan. No-op when the row is absent."""
        with self._lock:
            self._favorites.pop(member_id, None)

    @staticmethod
    def _serve_user_device(device_id: str, record: dict[str, Any]) -> dict[str, Any]:
        # Emit the forward shape with safe defaults so a new client always sees
        # the fields even for a LEGACY record written before the migration:
        #   control_entity_ids — alias of the stored entity_ids
        #   gangs — {} (the catalog falls back to its own derivation)
        #   room_id — None
        # The record's own keys (via **record) win when present.
        # control_entity_ids is DERIVED from the stored entity_ids at serve time
        # — the v3 migration does NOT rename the stored key (records keep
        # "entity_ids").
        return {
            "ha_device_id": device_id,
            "control_entity_ids": list(record.get("entity_ids", ())),
            "gangs": record.get("gangs", {}),
            "room_id": record.get("room_id"),
            **record,
        }

    def list_user_devices(self) -> list[dict[str, Any]]:
        return [
            self._serve_user_device(device_id, record)
            for device_id, record in self._user_devices.items()
        ]

    def get_user_device(self, ha_device_id: str) -> dict[str, Any]:
        record = self._user_devices.get(ha_device_id)
        if record is None:
            raise UnknownItemError("Unknown device")
        return self._serve_user_device(ha_device_id, record)

    def upsert_user_device(
        self,
        ha_device_id: Any,
        *,
        entity_ids: Any = None,
        control_entity_ids: Any = None,
        gang_types: Any = None,
        gang_names: Any = None,
        gangs: Any = None,
        config_entity_ids: Any = None,
        device_type: Any = None,
        custom_name: Any = None,
        custom_icon: Any = None,
        room_id: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(ha_device_id, str) or not ha_device_id.strip():
            raise RegistryError("ha_device_id is required")
        controls = entity_ids if control_entity_ids is None else control_entity_ids
        record = {
            "entity_ids": _clean_entity_ids(controls),
            "gang_types": _clean_gang_map(gang_types, "gang_types"),
            "gang_names": _clean_gang_map(gang_names, "gang_names"),
            "gangs": _clean_gangs(gangs),
            "config_entity_ids": _clean_entity_ids(
                config_entity_ids or [], "config_entity_ids"
            ),
            "device_type": _clean_device_type(device_type),
            "custom_name": _clean_optional_name(custom_name),
            "custom_icon": _clean_icon(custom_icon),
            "room_id": _clean_optional_room(room_id),
        }

        record["gangs"] = _gangs_backed_by(record["gangs"], record["entity_ids"])
        with self._lock:
            previous = self._user_devices.get(ha_device_id) or {}
            self._retain_room_overrides(record["gangs"], previous.get("gangs", {}))

            self._reject_grabbed_elsewhere(ha_device_id, record)
            self._user_devices[ha_device_id] = record
        _LOGGER.info(
            "Registry: user-device %s upserted (%d entities)",
            ha_device_id,
            len(record["entity_ids"]),
        )
        return self._serve_user_device(ha_device_id, record)

    def patch_user_device(
        self,
        ha_device_id: str,
        *,
        entity_ids: Any = ...,
        control_entity_ids: Any = ...,
        gang_types: Any = ...,
        gang_names: Any = ...,
        gangs: Any = ...,
        config_entity_ids: Any = ...,
        device_type: Any = ...,
        custom_name: Any = ...,
        custom_icon: Any = ...,
        room_id: Any = ...,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._user_devices.get(ha_device_id)
            if record is None:
                raise UnknownItemError("Unknown device")
            controls = entity_ids if control_entity_ids is ... else control_entity_ids
            if controls is not ...:
                new_ids = _clean_entity_ids(controls)

                dropped = [e for e in record["entity_ids"] if e not in new_ids]
                if dropped:
                    raise RegistryError(
                        f"entity_ids cannot drop a grabbed relay {dropped}; "
                        "hide the gang or delete the device"
                    )
                record["entity_ids"] = new_ids
            if gang_types is not ...:
                record["gang_types"] = _clean_gang_map(gang_types, "gang_types")
            if gang_names is not ...:
                record["gang_names"] = _clean_gang_map(gang_names, "gang_names")
            if gangs is not ...:
                cleaned = _clean_gangs(gangs)
                self._retain_room_overrides(cleaned, record.get("gangs", {}))
                record["gangs"] = cleaned
            if config_entity_ids is not ...:
                record["config_entity_ids"] = _clean_entity_ids(
                    config_entity_ids or [], "config_entity_ids"
                )
            if device_type is not ...:
                record["device_type"] = _clean_device_type(device_type)
            if custom_name is not ...:
                record["custom_name"] = _clean_optional_name(custom_name)
            if custom_icon is not ...:
                record["custom_icon"] = _clean_icon(custom_icon)
            if room_id is not ...:
                record["room_id"] = _clean_optional_room(room_id)

            record["gangs"] = _gangs_backed_by(record["gangs"], record["entity_ids"])
            if controls is not ... or config_entity_ids is not ...:
                self._reject_grabbed_elsewhere(ha_device_id, record)
            self._user_devices[ha_device_id] = record
        return self._serve_user_device(ha_device_id, record)

    def _reject_grabbed_elsewhere(
        self, ha_device_id: str, record: dict[str, Any]
    ) -> None:
        """One owner per entity: refuse ``record`` when another device already
        grabbed one of its control or config entities. The device itself may
        keep or reshuffle its own. Call under the lock, before storing."""
        taken: set[str] = set()
        for other_id, other in self._user_devices.items():
            if other_id == ha_device_id:
                continue
            taken.update(other.get("entity_ids", ()))
            taken.update(other.get("config_entity_ids", ()))
        clash = [
            e
            for e in (*record["entity_ids"], *record.get("config_entity_ids", ()))
            if e in taken
        ]
        if clash:
            raise RegistryError(f"entities already grabbed by another device: {clash}")

    def _mutate_gang(
        self, ha_device_id: str, gang_key: str, mutate: Any
    ) -> dict[str, Any]:
        """Read-modify-write ONE gang under the lock. ``UnknownItemError`` for an
        absent device or gang. ``mutate`` validates + sets fields on a COPY of the
        gang dict, so a rejected value (its ``RegistryError`` -> 400) leaves the
        stored record untouched."""
        with self._lock:
            record = self._user_devices.get(ha_device_id)
            if record is None:
                raise UnknownItemError("Unknown device")
            gangs = record.get("gangs")
            if not isinstance(gangs, dict) or gang_key not in gangs:
                raise UnknownItemError("Unknown gang")
            gang = dict(gangs[gang_key])
            mutate(gang)  # validates; may raise RegistryError before we persist
            new_gangs = dict(gangs)
            new_gangs[gang_key] = gang
            record = {**record, "gangs": new_gangs}
            self._user_devices[ha_device_id] = record  # persist
        return self._serve_user_device(ha_device_id, record)

    def set_gang_presentation(
        self, ha_device_id: str, gang_key: str, presentation: Any
    ) -> dict[str, Any]:
        """Flip a gang's presentation — promote (grouped->solo), delete-the-solo
        (solo->grouped), hide (any->hidden), un-hide (hidden->grouped). Every
        operation is a validated assignment of the single presentation field."""

        def mutate(gang: dict[str, Any]) -> None:
            if (
                not isinstance(presentation, str)
                or presentation not in _VALID_GANG_PRESENTATIONS
            ):
                raise RegistryError("gang presentation must be grouped, solo or hidden")
            gang["presentation"] = presentation

        return self._mutate_gang(ha_device_id, gang_key, mutate)

    def set_gang_type(
        self, ha_device_id: str, gang_key: str, gang_type: Any
    ) -> dict[str, Any]:
        """Re-type a gang against the known relay-presentation set."""

        def mutate(gang: dict[str, Any]) -> None:
            gang["type"] = _clean_gang_type(gang_type)

        return self._mutate_gang(ha_device_id, gang_key, mutate)

    def set_gang_name_icon(
        self,
        ha_device_id: str,
        gang_key: str,
        *,
        name: Any = ...,
        icon: Any = ...,
    ) -> dict[str, Any]:
        """Relabel a gang. ``...`` leaves a field unchanged; an explicit None
        clears the name or icon."""

        def mutate(gang: dict[str, Any]) -> None:
            if name is not ...:
                if name is not None and not isinstance(name, str):
                    raise RegistryError("gang name must be a string or null")
                gang["name"] = name
            if icon is not ...:
                gang["icon"] = _clean_icon(icon)

        return self._mutate_gang(ha_device_id, gang_key, mutate)

    def set_gang_room(
        self, ha_device_id: str, gang_key: str, room_id: Any
    ) -> dict[str, Any]:

        def mutate(gang: dict[str, Any]) -> None:
            gang["room_id"] = _clean_optional_room(room_id)
            gang["room_override"] = True

        return self._mutate_gang(ha_device_id, gang_key, mutate)

    @staticmethod
    def _retain_room_overrides(gangs, previous):
        # Older clients do not know this additive field. A rename/full PUT
        # must not turn an explicitly Unassigned gang back into inheritance.
        for key, gang in gangs.items():
            if previous.get(key, {}).get("room_override") is True:
                gang["room_override"] = True

    def delete_user_device(self, ha_device_id: str) -> None:
        """Remove a grouped device — its entities become un-grabbed and
        re-appear in the add-devices list."""
        with self._lock:
            if ha_device_id not in self._user_devices:
                raise UnknownItemError("Unknown device")
            del self._user_devices[ha_device_id]
        _LOGGER.info("Registry: user-device %s deleted", ha_device_id)

    def grabbed_entity_ids(self) -> set[str]:
        """Every entity grabbed into a user-device — primary gangs AND config
        entities. The add-devices list is the served set MINUS this."""
        grabbed: set[str] = set()
        for record in self._user_devices.values():
            grabbed.update(
                record.get("control_entity_ids") or record.get("entity_ids", ())
            )
            grabbed.update(record.get("config_entity_ids", ()))
        return grabbed

    def import_initial(
        self,
        floors: list[dict[str, Any]],
        rooms: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Seed the registry from HA's own registries on first setup.

        Imported floors/rooms KEEP their HA ids — room-scope JWT claims
        already use HA area ids, so imported layouts work with existing
        scoped tokens unchanged. Existing records are never overwritten
        (re-running an import can't clobber installer edits). Names are
        cleaned LENIENTLY (truncate, fall back — never raise): a weird
        HA name must not be able to abort integration setup.
        """
        counts = {"floors": 0, "rooms": 0, "assignments": 0}
        with self._lock:
            for floor in floors:
                floor_id = floor.get("floor_id")
                if not isinstance(floor_id, str) or not floor_id:
                    # No usable id -> can't be stored; skip the record, never
                    # abort the seed (same lenient posture as names).
                    continue
                if floor_id in self._floors:
                    continue
                self._floors[floor_id] = {
                    "name": _lenient_name(floor.get("name"), floor_id),
                    "sort_order": _lenient_sort_order(floor.get("sort_order")),
                }
                counts["floors"] += 1
            for room in rooms:
                room_id = room["room_id"]
                if room_id in self._rooms:
                    continue
                floor_id = room.get("floor_id")
                icon = room.get("icon")
                record = {
                    "name": _lenient_name(room.get("name"), room_id),
                    # area.floor_id is None for any HA area not on a floor —
                    # the NORMAL shape for apartments. A floorless room is
                    # valid; only a dangling reference gets cleared.
                    "floor_id": floor_id
                    if isinstance(floor_id, str) and floor_id in self._floors
                    else None,
                    # Same lenient posture: a bad HA icon is dropped, not fatal.
                    "icon": icon
                    if isinstance(icon, str) and 0 < len(icon) <= _ICON_MAX
                    else None,
                    "sort_order": _lenient_sort_order(room.get("sort_order")),
                }
                self._rooms[room_id] = record
                with self._mirror_lock:
                    self._room_names[room_id] = record["name"]
                counts["rooms"] += 1
            for assignment in assignments:
                entity_id = assignment["entity_id"]
                room_id = assignment.get("room_id")
                if entity_id in self._devices or room_id not in self._rooms:
                    continue
                record = {
                    "room_id": room_id,
                    "display_name": None,
                    "sort_order": 0,
                }
                self._devices[entity_id] = record
                self._mirror_assignment(entity_id, record)
                counts["assignments"] += 1
        _LOGGER.info(
            "Registry import: %(floors)d floors, %(rooms)d rooms, "
            "%(assignments)d assignments",
            counts,
        )
        return counts
