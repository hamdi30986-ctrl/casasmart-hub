"""Per-member settings that roam across a member's phones.

Stores the display name and the home-screen widget layout, keyed by
member_id like registry favorites. No HA imports; the storage methods block,
so call them through the executor.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

_NAME_MAX = 64
# Well above a 4x4 widget grid, but keeps the row bounded.
_MAX_TILES = 64
_TILE_FIELD_MAX = 128


class SettingsError(Exception):
    """Settings input rejected (maps to HTTP 400)."""


def _clean_display_name(value: Any) -> str | None:
    """None or blank clears the name; anything else must be a short string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError("display_name must be a string or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > _NAME_MAX:
        raise SettingsError(f"display_name is too long (max {_NAME_MAX})")
    return cleaned


def _clean_widget_tiles(value: Any) -> list[dict[str, str]] | None:
    """None clears the layout; otherwise a list of {type, entityId, name} tiles."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise SettingsError("widget_tiles must be a list or null")
    if len(value) > _MAX_TILES:
        raise SettingsError(f"At most {_MAX_TILES} widget tiles")
    cleaned: list[dict[str, str]] = []
    for tile in value:
        if not isinstance(tile, dict):
            raise SettingsError("Each widget tile must be an object")
        entry: dict[str, str] = {}
        for field in ("type", "entityId", "name"):
            raw = tile.get(field)
            if not isinstance(raw, str) or len(raw) > _TILE_FIELD_MAX:
                raise SettingsError(
                    f"Widget tile {field} must be a string of at most "
                    f"{_TILE_FIELD_MAX} chars"
                )
            entry[field] = raw
        if not entry["type"] or not entry["entityId"]:
            raise SettingsError("Widget tile needs type and entityId")
        cleaned.append(entry)
    return cleaned


# Every settable field and its validator; an update naming anything else is
# rejected.
_VALIDATORS = {
    "display_name": _clean_display_name,
    "widget_tiles": _clean_widget_tiles,
}


class UserSettingsEngine:
    """Per-user settings rows over one storage table."""

    def __init__(self, table: Any) -> None:
        self._table = table
        self._lock = threading.RLock()

    def get(self, member_id: str) -> dict[str, Any]:
        """A member's settings, with None for each unset field."""
        record = self._table.get(member_id) or {}
        return {field: record.get(field) for field in _VALIDATORS}

    def update(self, member_id: str, changes: Any) -> dict[str, Any]:
        """Apply a partial update and return the member's full settings.

        Only the fields in changes are touched and null clears one. An
        unknown field raises SettingsError.
        """
        if not isinstance(changes, dict):
            raise SettingsError("Body must be a JSON object")
        unknown = [key for key in changes if key not in _VALIDATORS]
        if unknown:
            raise SettingsError(f"Unknown settings field: {unknown[0]!r}")
        if not changes:
            raise SettingsError(
                "Nothing to change: provide display_name and/or widget_tiles"
            )
        validated = {
            field: _VALIDATORS[field](value) for field, value in changes.items()
        }
        with self._lock:
            record = self._table.get(member_id) or {}
            record.update(validated)
            # A row with every field cleared is deleted.
            if all(record.get(field) is None for field in record):
                self._table.pop(member_id, None)
            else:
                self._table[member_id] = record
        return {field: record.get(field) for field in _VALIDATORS}

    def delete(self, member_id: str) -> None:
        """Drop a member's settings, if any (when their last device unpairs)."""
        with self._lock:
            self._table.pop(member_id, None)
