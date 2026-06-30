"""CasaSmart device registry engine (Track B — B17 hub side).

The hub-local source of truth for the home's *organization* — what
Supabase used to hold (plan B17: "All device/room/floor data moves from
Supabase to hub-local storage"):

- **Floors** — name + sort order.
- **Rooms** — name, floor, icon, sort order. A room's id doubles as the
  area id used in room-scope JWT claims; rooms imported from HA keep
  their HA area id so existing scoped tokens keep working.
- **Device assignments** — entity_id -> room + display name + sort
  order. ``room_id = None`` is an *explicit* "Unassigned" (it blocks the
  HA-area fallback); an entity with no record at all falls back to HA's
  own area registry in ``filtering``.
- **Scenes** — name, icon, and a list of whitelisted device commands.
  Every command is validated against the SAME entity-bridge whitelist
  the command endpoint uses — a scene can never store an action the app
  couldn't send directly.
- **Favorites** — per user (= per enrolled device id, the only user
  identity the hub has). One table, replacing the app's three sources
  of truth (plan: SharedPreferences + two Supabase columns collapse
  into this).

Like ``auth_engine``: no Home Assistant imports — only the dict-like
storage tables, so the whole thing unit-tests on a temp SQLite file.
Storage-touching methods are synchronous (call via executor). The
in-memory mirror (``room_of`` / ``room_name``) exists for the hot path:
``filtering`` resolves a room on the event loop for EVERY pushed state
change, and that must never hop to SQLite.
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import Any

try:
    from .entity_bridge import CommandError, validate_command
except ImportError:  # top-level import in the test env (no HA package init)
    from entity_bridge import CommandError, validate_command  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# room_of() return for "no assignment record" — distinct from None, which
# means an explicit Unassigned that must NOT fall back to the HA area.
UNSET = object()

_NAME_MAX = 64
_ICON_MAX = 64
_MAX_FAVORITES = 200
_MAX_SCENE_ENTITIES = 50
_MAX_DEVICE_ENTITIES = 100


class RegistryError(Exception):
    """Registry input rejected (maps to HTTP 400)."""


class UnknownItemError(RegistryError):
    """No floor/room/scene/assignment under that id (maps to HTTP 404)."""


class InUseError(RegistryError):
    """Deletion refused because something still references the item."""


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


def _clean_favorite(favorite: Any) -> bool:
    """House-wide scene-favorite flag. Must be a real bool when present."""
    if not isinstance(favorite, bool):
        raise RegistryError("favorite must be a boolean")
    return favorite


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

# A gang's `type` drives only the card icon + which tab it lands on — it NEVER
# changes how the gang is controlled (always the relay's real domain). It is a
# CLOSED set of relay-presentation categories, distinct from the free-form
# device-level `device_type` (a SmartDevice enum name like wallSwitch/sensorMotion).
_KNOWN_GANG_TYPES = frozenset({"switch", "light", "fan", "heater", "outlet"})


def _clean_gang_type(value: Any) -> str:
    """Validate a gang's presentation type against the known set."""
    if not isinstance(value, str) or value not in _KNOWN_GANG_TYPES:
        raise RegistryError(
            "gang type must be one of: " + ", ".join(sorted(_KNOWN_GANG_TYPES))
        )
    return value


def _clean_gangs(value: Any) -> dict[str, dict[str, Any]]:
    """A ``{control_entity_id -> {type, icon?, name?, presentation}}`` map;
    None -> {}. presentation defaults to 'grouped'; an absent type defaults to
    'switch'.

    Migration Phase 3 turns on enforcement: a PROVIDED ``type`` must be in the
    known set (``_clean_gang_type``), and ``presentation`` must be one of
    {grouped, solo, hidden}. The exactly-one-of invariant is intrinsic (a gang
    holds a single presentation value); the dedicated gang commands flip it."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError("gangs must be a map")
    out: dict[str, dict[str, Any]] = {}
    for key, gang in value.items():
        if not isinstance(key, str) or not isinstance(gang, dict):
            raise RegistryError("gangs entries must be {entity_id: {...}}")
        presentation = gang.get("presentation", "grouped")
        if presentation not in _VALID_GANG_PRESENTATIONS:
            raise RegistryError(
                "gang presentation must be grouped, solo or hidden"
            )
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
        raise RegistryError(
            f"A scene may hold at most {_MAX_SCENE_ENTITIES} entities"
        )
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
        cleaned.append(
            {
                "entity_id": entity_id,
                "action": item["action"],
                "data": item.get("data") or {},
            }
        )
    return cleaned


class RegistryEngine:
    """Floors, rooms, device assignments, grouped user-devices, scenes,
    per-user favorites."""

    def __init__(
        self,
        floors_table: Any,
        rooms_table: Any,
        devices_table: Any,
        scenes_table: Any,
        favorites_table: Any,
        user_devices_table: Any,
    ) -> None:
        self._floors = floors_table
        self._rooms = rooms_table
        self._devices = devices_table
        self._scenes = scenes_table
        self._favorites = favorites_table
        self._user_devices = user_devices_table
        # Serializes storage mutations (held across SQLite I/O).
        self._lock = threading.RLock()
        # Guards ONLY the in-memory mirrors below — held for dict ops,
        # never across storage I/O, so the event-loop reads can never
        # block behind a SQLite write (they run for every pushed state
        # change). Mutations update the mirror AFTER the DB write lands,
        # so the mirror never gets ahead of what's persisted.
        self._mirror_lock = threading.Lock()
        # Event-loop mirrors — kept in sync by every mutation below.
        # entity_id -> (room_id|None, display_name|None); room None =
        # explicit Unassigned.
        self._assignment_cache: dict[str, tuple[str | None, str | None]] = {}
        # room_id -> room name.
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

    # -- event-loop reads (pure memory — used by filtering on every push) ------

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

    # -- floors (storage — call via executor) -----------------------------------

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

    # -- rooms (storage — call via executor) -------------------------------------

    def list_rooms(self) -> list[dict[str, Any]]:
        return [
            {"room_id": room_id, **record}
            for room_id, record in self._rooms.items()
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
        """Delete a room; its devices become explicitly Unassigned.
        Returns how many assignments were cleared."""
        with self._lock:
            if room_id not in self._rooms:
                raise UnknownItemError("Unknown room")
            cleared = 0
            for entity_id, record in list(self._devices.items()):
                if record.get("room_id") == room_id:
                    record["room_id"] = None
                    self._devices[entity_id] = record
                    # Mirror follows each landed write — a mid-loop
                    # failure leaves mirror == DB for what was written.
                    self._mirror_assignment(entity_id, record)
                    cleared += 1
            del self._rooms[room_id]
        with self._mirror_lock:
            self._room_names.pop(room_id, None)
        _LOGGER.info(
            "Registry: room %s deleted (%d device(s) unassigned)", room_id, cleared
        )
        return cleared

    def _checked_floor_id(self, floor_id: Any) -> str | None:
        if floor_id is None:
            return None
        if not isinstance(floor_id, str) or floor_id not in self._floors:
            raise RegistryError("Unknown floor_id")
        return floor_id

    # -- device assignments (storage — call via executor) -------------------------

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
        """Create/update an assignment record. ``...`` = leave unchanged;
        ``room_id=None`` is the explicit Unassigned pool; an empty/None
        display_name falls back to the HA friendly name at read time."""
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
                    # Empty/whitespace = clear (fall back to HA friendly name).
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

    # -- scenes (storage — call via executor) -------------------------------------

    @staticmethod
    def _scene_out(scene_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Public scene shape. ``favorite`` defaults False for legacy
        records that predate the house-wide favorites flag."""
        return {
            "scene_id": scene_id,
            **record,
            "favorite": bool(record.get("favorite", False)),
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
        self, name: Any, entities: Any, icon: Any = None
    ) -> dict[str, Any]:
        record = {
            "name": _clean_name(name, "Scene"),
            "icon": _clean_icon(icon),
            "entities": _clean_scene_entities(entities),
            "favorite": False,
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
            self._scenes[scene_id] = record  # persist
        return self._scene_out(scene_id, record)

    def delete_scene(self, scene_id: str) -> None:
        with self._lock:
            try:
                del self._scenes[scene_id]
            except KeyError:
                raise UnknownItemError("Unknown scene") from None
        _LOGGER.info("Registry: scene %s deleted", scene_id)

    # -- favorites (storage — call via executor) -----------------------------------

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

    # -- grouped user-devices (storage — call via executor) --------------------
    # The hub-side source of truth for a physical device's STRUCTURE: which
    # entities were grabbed, the per-gang type (light/switch/fan), gang names,
    # the config entities (the device's Settings sheet drives them), the device
    # type, and custom name/icon. Room stays per-entity (assign_device), so
    # these records carry no room. "Grabbed" = every entity referenced here, so
    # the add-devices list is the served set MINUS grabbed_entity_ids().

    @staticmethod
    def _serve_user_device(
        device_id: str, record: dict[str, Any]
    ) -> dict[str, Any]:
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
        """Create or fully replace a grouped-device record (import / re-import).

        ``control_entity_ids`` is the forward name for ``entity_ids`` (the gang
        relays); either is accepted. ``gangs`` is the nested per-gang
        presentation model keyed by control entity_id; ``room_id`` is the
        device-level room. Legacy flat ``gang_types``/``gang_names`` are still
        accepted through the migration window."""
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
        # A gang must map to a grabbed relay — drop any orphan.
        record["gangs"] = _gangs_backed_by(record["gangs"], record["entity_ids"])
        with self._lock:
            # A relay can't be grabbed by two devices: reject if any of this
            # record's entities is already owned by a DIFFERENT record. A
            # re-import of the same ha_device_id replaces its own record (exempt);
            # a fresh grab of an in-use relay is the double-grab the model forbids.
            taken: set[str] = set()
            for other_id, other in self._user_devices.items():
                if other_id == ha_device_id:
                    continue
                taken.update(other.get("entity_ids", ()))
                taken.update(other.get("config_entity_ids", ()))
            clash = [
                e
                for e in (*record["entity_ids"], *record["config_entity_ids"])
                if e in taken
            ]
            if clash:
                raise RegistryError(
                    f"entities already grabbed by another device: {clash}"
                )
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
        """Edit a grouped-device record. ``...`` means "leave unchanged"
        (rename a gang, re-type, set an icon); an explicit None clears a
        nullable field. ``control_entity_ids`` is the forward alias of
        ``entity_ids``."""
        with self._lock:
            record = self._user_devices.get(ha_device_id)
            if record is None:
                raise UnknownItemError("Unknown device")
            controls = (
                entity_ids if control_entity_ids is ... else control_entity_ids
            )
            if controls is not ...:
                new_ids = _clean_entity_ids(controls)
                # A PATCH must never DROP a grabbed relay: that would free one
                # relay into the add-list while the device stays grabbed. Relay
                # removal is hide-the-gang or delete-the-device only; growing the
                # set (re-discovery) is fine. A full re-import goes through the
                # PUT/upsert path, not here.
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
                record["gangs"] = _clean_gangs(gangs)
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
            # A gang must map to a grabbed relay — drop any orphan (e.g. a
            # gangs= patch referencing a non-relay).
            record["gangs"] = _gangs_backed_by(record["gangs"], record["entity_ids"])
            self._user_devices[ha_device_id] = record  # persist
        return self._serve_user_device(ha_device_id, record)

    # -- per-gang presentation commands (storage — call via executor) ----------
    # Flip ONE gang of a grouped record: promote/hide/un-hide (presentation),
    # re-type, or relabel (name/icon). These are PURE presentation metadata —
    # they never touch the device's entities or HA (control is always via the
    # relay's real domain). The exactly-one-of {grouped, solo, hidden} invariant
    # is intrinsic: a gang holds a single presentation value, so a flip is a
    # validated assignment with no forbidden transition.

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
            if presentation not in _VALID_GANG_PRESENTATIONS:
                raise RegistryError(
                    "gang presentation must be grouped, solo or hidden"
                )
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

    # -- first-run import (storage — call via executor) ------------------------------

    def import_initial(
        self,
        floors: list[dict[str, Any]],
        rooms: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Seed the registry from HA's own registries (B17: "Auto-populate
        from HA entities on first setup").

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
                floor_id = floor["floor_id"]
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
                    "floor_id": floor_id if floor_id in self._floors else None,
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
