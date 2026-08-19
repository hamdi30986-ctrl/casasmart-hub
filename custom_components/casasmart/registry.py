"""CasaSmart runtime component."""

from __future__ import annotations

import logging
import secrets
import threading
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


class RegistryError(Exception):
    """CasaSmart runtime component."""


class UnknownItemError(RegistryError):
    """CasaSmart runtime component."""


class InUseError(RegistryError):
    """CasaSmart runtime component."""


def _clean_name(name: Any, what: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RegistryError(f"{what} name is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise RegistryError(f"{what} name is too long (max {_NAME_MAX})")
    return cleaned


def _lenient_name(name: Any, fallback: str) -> str:
    """CasaSmart runtime component."""
    if not isinstance(name, str) or not name.strip():
        return fallback
    return name.strip()[:_NAME_MAX]


def _lenient_sort_order(sort_order: Any) -> int:
    """CasaSmart runtime component."""
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

    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise RegistryError("sort_order must be an integer")
    return sort_order


def _clean_favorite(favorite: Any) -> bool:
    """CasaSmart runtime component."""
    if not isinstance(favorite, bool):
        raise RegistryError("favorite must be a boolean")
    return favorite


def _clean_energy_flag(value: Any) -> bool:
    """CasaSmart runtime component."""
    if not isinstance(value, bool):
        raise RegistryError("works_during_energy_saving must be a boolean")
    return value


def _clean_entity_ids(
    value: Any, what: str = "entity_ids", max_count: int = _MAX_DEVICE_ENTITIES
) -> list[str]:
    """CasaSmart runtime component."""
    if not isinstance(value, list) or any(
        not isinstance(eid, str) or "." not in eid for eid in value
    ):
        raise RegistryError(f"{what} must be a list of entity_id strings")
    if len(value) > max_count:
        raise RegistryError(f"At most {max_count} {what}")
    return list(dict.fromkeys(value))


def _clean_gang_map(value: Any, what: str) -> dict[str, str]:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    if not isinstance(value, str) or value not in _KNOWN_GANG_TYPES:
        raise RegistryError(
            "gang type must be one of: " + ", ".join(sorted(_KNOWN_GANG_TYPES))
        )
    return value


def _clean_gangs(value: Any) -> dict[str, dict[str, Any]]:
    """CasaSmart runtime component."""
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
            "room_id": _clean_optional_room(gang.get("room_id")),
        }
    return out


def _gangs_backed_by(
    gangs: dict[str, dict[str, Any]], entity_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """CasaSmart runtime component."""
    allowed = set(entity_ids)
    return {key: gang for key, gang in gangs.items() if key in allowed}


def _clean_optional_room(value: Any) -> str | None:
    """CasaSmart runtime component."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RegistryError("room_id must be a non-empty string or null")
    return value


def _clean_optional_name(name: Any) -> str | None:
    """CasaSmart runtime component."""
    return None if name is None else _clean_name(name, "Device")


def _clean_device_type(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _NAME_MAX:
        raise RegistryError("device_type must be a string")
    return value


def _clean_scene_entities(entities: Any) -> list[dict[str, Any]]:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""

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

        self._lock = threading.RLock()





        self._mirror_lock = threading.Lock()



        self._assignment_cache: dict[str, tuple[str | None, str | None]] = {}

        self._room_names: dict[str, str] = {}

    def warm_up(self) -> None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        with self._mirror_lock:
            cached = self._assignment_cache.get(entity_id)
        return UNSET if cached is None else cached[0]

    def display_name_of(self, entity_id: str) -> str | None:
        """CasaSmart runtime component."""
        with self._mirror_lock:
            cached = self._assignment_cache.get(entity_id)
        return None if cached is None else cached[1]

    def room_name(self, room_id: str) -> str | None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        with self._lock:
            record = self._floors.get(floor_id)
            if record is None:
                raise UnknownItemError("Unknown floor")
            if name is not ...:
                record["name"] = _clean_name(name, "Floor")
            if sort_order is not ...:
                record["sort_order"] = _clean_sort_order(sort_order)
            self._floors[floor_id] = record
        return {"floor_id": floor_id, **record}

    def delete_floor(self, floor_id: str) -> None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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
            self._rooms[room_id] = record
        with self._mirror_lock:
            self._room_names[room_id] = record["name"]
        return {"room_id": room_id, **record}

    def delete_room(self, room_id: str) -> int:
        """CasaSmart runtime component."""
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



    def list_assignments(self) -> dict[str, dict[str, Any]]:
        """CasaSmart runtime component."""
        return dict(self._devices.items())

    def assign_device(
        self,
        entity_id: str,
        room_id: Any = ...,
        display_name: Any = ...,
        sort_order: Any = ...,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
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

    def remove_assignment(self, entity_id: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            try:
                del self._devices[entity_id]
            except KeyError:
                raise UnknownItemError("No assignment for that entity") from None
        with self._mirror_lock:
            self._assignment_cache.pop(entity_id, None)



    @staticmethod
    def _scene_out(scene_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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
            self._scenes[scene_id] = record
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
        """CasaSmart runtime component."""
        deduped = _clean_entity_ids(entity_ids, "favorites", _MAX_FAVORITES)
        with self._lock:
            self._favorites[member_id] = {"entity_ids": deduped}
        return deduped

    def delete_favorites(self, member_id: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            self._favorites.pop(member_id, None)









    @staticmethod
    def _serve_user_device(
        device_id: str, record: dict[str, Any]
    ) -> dict[str, Any]:









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
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        with self._lock:
            record = self._user_devices.get(ha_device_id)
            if record is None:
                raise UnknownItemError("Unknown device")
            controls = (
                entity_ids if control_entity_ids is ... else control_entity_ids
            )
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


            record["gangs"] = _gangs_backed_by(record["gangs"], record["entity_ids"])
            self._user_devices[ha_device_id] = record
        return self._serve_user_device(ha_device_id, record)









    def _mutate_gang(
        self, ha_device_id: str, gang_key: str, mutate: Any
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            record = self._user_devices.get(ha_device_id)
            if record is None:
                raise UnknownItemError("Unknown device")
            gangs = record.get("gangs")
            if not isinstance(gangs, dict) or gang_key not in gangs:
                raise UnknownItemError("Unknown gang")
            gang = dict(gangs[gang_key])
            mutate(gang)
            new_gangs = dict(gangs)
            new_gangs[gang_key] = gang
            record = {**record, "gangs": new_gangs}
            self._user_devices[ha_device_id] = record
        return self._serve_user_device(ha_device_id, record)

    def set_gang_presentation(
        self, ha_device_id: str, gang_key: str, presentation: Any
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""

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
        """CasaSmart runtime component."""

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
        """CasaSmart runtime component."""

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
        """CasaSmart runtime component."""

        def mutate(gang: dict[str, Any]) -> None:
            gang["room_id"] = _clean_optional_room(room_id)

        return self._mutate_gang(ha_device_id, gang_key, mutate)

    def delete_user_device(self, ha_device_id: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            if ha_device_id not in self._user_devices:
                raise UnknownItemError("Unknown device")
            del self._user_devices[ha_device_id]
        _LOGGER.info("Registry: user-device %s deleted", ha_device_id)

    def grabbed_entity_ids(self) -> set[str]:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        counts = {"floors": 0, "rooms": 0, "assignments": 0}
        with self._lock:
            for floor in floors:
                floor_id = floor.get("floor_id")
                if not isinstance(floor_id, str) or not floor_id:


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



                    "floor_id": floor_id
                    if isinstance(floor_id, str) and floor_id in self._floors
                    else None,

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
