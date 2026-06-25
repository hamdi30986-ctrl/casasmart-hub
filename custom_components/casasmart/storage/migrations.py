"""Forward-only schema migrations with automatic backup/restore.

Versioning uses SQLite's ``PRAGMA user_version``. Before any migration runs,
the database is backed up via the SQLite online-backup API (WAL-safe). If a
migration fails, the original file is restored over the database — a failed
migration never leaves the schema half-applied.

Rules:
- Migrations are forward-only. A database whose version is HIGHER than the
  code's latest known version is refused (downgrade = undefined behavior).
- Each migration runs inside its own transaction and bumps ``user_version``
  atomically with its DDL/DML.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .exceptions import MigrationError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """One schema step: brings the database from ``version - 1`` to ``version``."""

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

    Tank telemetry is a high-frequency time series; the v1 shape stored it as a
    single per-device JSON blob in ``kv`` that was rewritten WHOLE on every
    5-minute reading (~270 KB x 288/day = tens of MB/day/device of flash wear).
    Move it to a real row-per-reading table so an ingest is one INSERT and
    retention is a bounded DELETE. Carry any existing history forward, then drop
    the old blob rows.
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


#: Ordered list of all known migrations. Append-only — never edit a shipped one.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial kv table", _migration_v1),
    Migration(2, "append-only tank_readings table", _migration_v2),
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}-v{{ver}}-{stamp}.db"

    # NOTE: sqlite3 connections must be closed explicitly — `with conn:` only
    # manages transactions, it does NOT close. An unclosed destination would
    # leave the backup's WAL un-checkpointed and the .db file incomplete.
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
    """Bring the database at ``db_path`` up to the latest schema version.

    Returns the resulting schema version. Raises MigrationError on failure;
    in that case the database file has already been restored from the backup
    taken before the run.
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
