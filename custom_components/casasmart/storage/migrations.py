"""Forward-only schema migrations with backup and restore.

The schema version is SQLite's PRAGMA user_version. The database is backed up
before any migration runs and restored if a step fails. Each step runs in one
transaction together with its user_version bump. A database newer than this
code is refused.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .exceptions import MigrationError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """One schema step, from version - 1 to version."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _migration_v1(conn: sqlite3.Connection) -> None:
    """Initial schema: the namespaced key-value table behind KeyValueTable."""
    conn.execute(
        """
        CREATE TABLE kv (
            namespace  TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (namespace, key)
        )
        """
    )


def _migration_v2(conn: sqlite3.Connection) -> None:
    """Append-only tank readings.

    v1 kept each device's history as one JSON blob in kv, rewritten on every
    5-minute reading (about 270 KB, 288 times a day). A row per reading makes
    an ingest one INSERT and retention a bounded DELETE. Existing history is
    copied over and the blob rows dropped.
    """
    conn.execute(
        """
        CREATE TABLE tank_readings (
            device_id TEXT NOT NULL,
            t         INTEGER NOT NULL,
            v         REAL NOT NULL,
            PRIMARY KEY (device_id, t)
        )
        """
    )
    # Backfill from the v1 kv blob (namespace 'tank_readings', one
    # {"entries": [{"t":.., "v":..}, ...]} row per device), then remove it.
    for device_id, blob in conn.execute(
        "SELECT key, value FROM kv WHERE namespace = 'tank_readings'"
    ).fetchall():
        try:
            entries = (json.loads(blob) or {}).get("entries", [])
        except (TypeError, ValueError):
            continue
        for entry in entries:
            try:
                t = int(entry["t"])
                v = float(entry["v"])
            except (KeyError, TypeError, ValueError):
                continue
            if v != v or v in (float("inf"), float("-inf")):
                continue  # drop any NaN/Inf the v1 path let through
            conn.execute(
                "INSERT OR IGNORE INTO tank_readings (device_id, t, v) "
                "VALUES (?, ?, ?)",
                (device_id, t, v),
            )
    conn.execute("DELETE FROM kv WHERE namespace = 'tank_readings'")


# A copy of registry._KNOWN_GANG_TYPES; a folded type outside it becomes
# "switch".
_V3_KNOWN_GANG_TYPES = frozenset({"switch", "light", "fan", "heater", "outlet"})

# The gang suffixes the app used as keys in legacy gang_types/gang_names.
_V3_GANG_SUFFIX_KEYS = (
    "left",
    "right",
    "center",
    "l1",
    "l2",
    "l3",
    "endpoint_1",
    "endpoint_2",
    "endpoint_3",
    "gang_1",
    "gang_2",
    "gang_3",
)


def _v3_suffix_token(entity_id: str) -> str | None:
    """The gang suffix entity_id ends with (switch.kitchen_left -> left), or None."""
    local = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    for key in _V3_GANG_SUFFIX_KEYS:
        if local.endswith(f"_{key}") or local == key:
            return key
    return None


def _migration_v3(conn: sqlite3.Connection) -> None:
    """Fold the legacy flat gang_types/gang_names maps into nested gangs.

    Each control entity of a typed relay record becomes a grouped gang. A
    record without gang_types (climate, cover, sensor, a native light) gets no
    gangs and keeps rendering by its domain. Legacy maps were keyed by
    entity_id, gang suffix or gang_N, so a gang's type comes from its entity_id
    key, then its suffix key, then the only value on a single-gang record, else
    "switch". Records that already have gangs are skipped.
    """
    rows = conn.execute(
        "SELECT key, value FROM kv WHERE namespace = 'registry_user_devices'"
    ).fetchall()
    for key, raw in rows:
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        existing = record.get("gangs")
        if isinstance(existing, dict) and existing:
            continue  # already folded
        gang_types = record.get("gang_types")
        if not isinstance(gang_types, dict) or not gang_types:
            continue  # not a typed relay device
        entity_ids = [
            eid for eid in (record.get("entity_ids") or []) if isinstance(eid, str)
        ]
        if not entity_ids:
            continue
        gang_names = record.get("gang_names")
        if not isinstance(gang_names, dict):
            gang_names = {}
        sole_type = next(iter(gang_types.values())) if len(gang_types) == 1 else None

        gangs: dict[str, dict] = {}
        for eid in entity_ids:
            suffix = _v3_suffix_token(eid)
            gtype = (
                gang_types.get(eid)
                or (gang_types.get(suffix) if suffix else None)
                or (sole_type if len(entity_ids) == 1 else None)
                or "switch"
            )
            if not isinstance(gtype, str) or gtype not in _V3_KNOWN_GANG_TYPES:
                gtype = "switch"
            gname = gang_names.get(eid) or (gang_names.get(suffix) if suffix else None)
            gangs[eid] = {
                "type": gtype,
                "icon": None,
                "name": gname if isinstance(gname, str) else None,
                "presentation": "grouped",
            }
        if gangs:
            record["gangs"] = gangs
            conn.execute(
                "UPDATE kv SET value = ? "
                "WHERE namespace = 'registry_user_devices' AND key = ?",
                (json.dumps(record), key),
            )


def _migration_v4(conn: sqlite3.Connection) -> None:
    """Append-only audit events for the Energy Saving engine.

    Energy config and state stay small JSON documents in kv. Events are a
    growing history, so they get their own table with time and kind indexes.
    """
    conn.execute(
        """
        CREATE TABLE energy_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            t         INTEGER NOT NULL,
            kind      TEXT NOT NULL,
            level     TEXT,
            entity_id TEXT,
            room_id   TEXT,
            data      TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_energy_events_t ON energy_events (t DESC, id DESC)")
    conn.execute(
        "CREATE INDEX idx_energy_events_kind_t ON energy_events (kind, t DESC, id DESC)"
    )


#: Every migration, in order. Append new ones; never edit a shipped one.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial kv table", _migration_v1),
    Migration(2, "append-only tank_readings table", _migration_v2),
    Migration(3, "fold flat gang maps into nested gangs", _migration_v3),
    Migration(4, "append-only energy events table", _migration_v4),
)

LATEST_VERSION = max(m.version for m in MIGRATIONS)


def get_user_version(conn: sqlite3.Connection) -> int:
    """Return the database's current schema version."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    """Copy the live database to a timestamped file using the online-backup API.

    Safe under WAL: sqlite3.Connection.backup() produces a consistent snapshot
    that includes uncheckpointed WAL pages.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}-v{{ver}}-{stamp}.db"

    # `with conn:` only manages transactions, so close both connections
    # explicitly; an unclosed destination can leave the backup incomplete.
    src = sqlite3.connect(db_path)
    try:
        version = get_user_version(src)
        backup_path = Path(str(backup_path).format(ver=version))
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _LOGGER.info("Database backed up to %s", backup_path)
    return backup_path


def restore_database(db_path: Path, backup_path: Path) -> None:
    """Replace the database file with a backup; drop stale WAL/SHM sidecars."""
    shutil.copy2(backup_path, db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        sidecar.unlink(missing_ok=True)
    _LOGGER.warning("Database restored from backup %s", backup_path)


def run_migrations(
    db_path: Path,
    backup_dir: Path,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Bring the database at db_path up to the latest schema version.

    Returns the resulting schema version. Raises MigrationError when the
    database is newer than this code (nothing is touched), or when a step
    fails, after restoring the backup taken before the run.
    """
    target = max(m.version for m in migrations)

    probe = sqlite3.connect(db_path)
    try:
        current = get_user_version(probe)
    finally:
        probe.close()

    if current == target:
        return current
    if current > target:
        raise MigrationError(
            f"Database schema v{current} is newer than this code supports "
            f"(v{target}). Refusing to run — update the integration instead."
        )

    backup_path = backup_database(db_path, backup_dir)
    pending = sorted(
        (m for m in migrations if m.version > current), key=lambda m: m.version
    )

    conn = sqlite3.connect(db_path)
    closed = False
    try:
        for migration in pending:
            _LOGGER.info(
                "Applying migration v%d: %s", migration.version, migration.description
            )
            try:
                with conn:  # one transaction per migration step
                    # sqlite3 only opens a transaction implicitly before DML;
                    # without BEGIN, DDL would commit as it runs.
                    conn.execute("BEGIN")
                    migration.apply(conn)
                    conn.execute(f"PRAGMA user_version = {migration.version}")
            except Exception as err:
                conn.close()
                closed = True
                restore_database(db_path, backup_path)
                raise MigrationError(
                    f"Migration v{migration.version} ({migration.description}) "
                    f"failed and the database was restored: {err}"
                ) from err
        return target
    finally:
        if not closed:
            conn.close()
