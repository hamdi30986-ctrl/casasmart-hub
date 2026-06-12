"""Per-user settings store (post-funeral mini-block MB-2) — the pure half.

Replaces Phase 5's phone-local interim for the two pieces of personal
state that should roam across a user's phones: the display name (the
greeting/profile name) and the home-screen widget layout. Keyed by the
JWT's ``sub`` — the enrolled device id, the only user identity the hub
has — exactly like B17 favorites; one table, partial updates, schema
deliberately open for whatever rides along later (the plan names
favorites-style extensions).

Flat-importable engine like ``registry.py``: no HA imports, dict-like
storage table in, unit-tests on a temp SQLite file. Storage-touching
methods are synchronous (call via executor).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

_NAME_MAX = 64
# A widget grid is 4x4-ish today; 64 leaves generous headroom while
# keeping a runaway client from growing the row unbounded.
_MAX_TILES = 64
_TILE_FIELD_MAX = 128

# The complete settable surface today. PUT bodies naming anything else
# are rejected — additions extend this map (with their own validator),
# they never get stored unvalidated.
_KNOWN_FIELDS = ("display_name", "widget_tiles")


class SettingsError(Exception):
    """Settings input rejected (maps to HTTP 400)."""


def _clean_display_name(value: Any) -> str | None:
    """None/empty clears; anything else must be a sane short string."""
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
    """None clears; otherwise a list of ``{type, entityId, name}`` tiles.

    The hub stores the layout opaquely for the app to mirror back — but
    shape-validated and size-capped, never raw client JSON.
    """
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


_VALIDATORS = {
    "display_name": _clean_display_name,
    "widget_tiles": _clean_widget_tiles,
}


class UserSettingsEngine:
    """Per-user settings rows over one storage table."""

    def __init__(self, table: Any) -> None:
        self._table = table
        self._lock = threading.RLock()

    def get(self, user_device_id: str) -> dict[str, Any]:
        """The user's full settings doc — every known field, None when unset."""
        record = self._table.get(user_device_id) or {}
        return {field: record.get(field) for field in _KNOWN_FIELDS}

    def update(self, user_device_id: str, changes: Any) -> dict[str, Any]:
        """Partial update: only the fields present in ``changes`` move;
        an explicit null clears. Unknown fields are rejected so a typo'd
        key can't silently store garbage forever. Returns the full doc."""
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
            record = self._table.get(user_device_id) or {}
            record.update(validated)
            # Fully cleared rows are deleted, not kept as tombstones.
            if all(record.get(field) is None for field in record):
                self._table.pop(user_device_id, None)
            else:
                self._table[user_device_id] = record
        return {field: record.get(field) for field in _KNOWN_FIELDS}
