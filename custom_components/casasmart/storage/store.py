"""HubStorage — SQLite+WAL engine behind a dict-like wrapper.

The abstraction is the contract: nothing above this module ever touches
SQLite directly. Callers get named tables that behave like dicts of
JSON-serializable values:

    storage = HubStorage(Path("/config/casasmart/hub.db"))
    storage.open()
    users = storage.table("users")
    users["u-123"] = {"name": "Alex", "role": "admin"}
    users["u-123"]            # -> {'name': 'Alex', 'role': 'admin'}
    "u-123" in users          # -> True
    del users["u-123"]
    storage.close()

Thread-safety: a single connection guarded by an RLock. HA's asyncio side
must call through an executor (the integration scaffold in B1.2 wraps this).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

from .exceptions import StorageError
from .migrations import MIGRATIONS, Migration, get_user_version, run_migrations

_LOGGER = logging.getLogger(__name__)

_VALID_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Wait up to 5s on a locked database before failing instead of erroring
# immediately — covers brief contention from backups or a second reader.
_BUSY_TIMEOUT_MS = 5000


class HubStorage:
    """Owns the SQLite connection, WAL setup, and migration run."""

    def __init__(self, db_path: Path, backup_dir: Path | None = None) -> None:
        self._db_path = Path(db_path)
        self._backup_dir = (
            Path(backup_dir) if backup_dir else self._db_path.parent / "backups"
        )
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._tables: dict[str, KeyValueTable] = {}

    # -- lifecycle -----------------------------------------------------------

    def open(self, migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
        """Open the database: run migrations, enable WAL, verify it stuck."""
        if self._conn is not None:
            return

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_version = run_migrations(self._db_path, self._backup_dir, migrations)

        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if journal_mode.lower() != "wal":
                raise StorageError(
                    f"Could not enable WAL (journal_mode={journal_mode!r}). "
                    "Is the database on a filesystem that supports it?"
                )
            # synchronous=FULL: every COMMIT fsyncs the WAL, so a host power cut
            # can't drop an acknowledged write (a pairing, a registry edit, a
            # tank reading). NORMAL fsyncs only at checkpoint, which on a
            # low-write hub can lag far behind. Affordable here because writes
            # are small + infrequent — the tank readings table turned the one
            # high-frequency writer into a one-row INSERT, not a 270 KB blob.
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            conn.close()
            raise

        self._conn = conn
        _LOGGER.info(
            "Storage open: %s (schema v%d, journal=%s)",
            self._db_path,
            schema_version,
            journal_mode,
        )

    def close(self) -> None:
        """Checkpoint the WAL and close the connection."""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as err:
                _LOGGER.warning("WAL checkpoint on close failed: %s", err)
            self._conn.close()
            self._conn = None
            self._tables.clear()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return get_user_version(self._connection)

    # -- table access --------------------------------------------------------

    def table(self, namespace: str) -> "KeyValueTable":
        """Return the dict-like view for ``namespace`` (created lazily)."""
        if not _VALID_NAMESPACE.match(namespace):
            raise ValueError(
                f"Invalid table namespace {namespace!r}: must match "
                "[a-z][a-z0-9_]{0,63}"
            )
        if namespace not in self._tables:
            self._tables[namespace] = KeyValueTable(self, namespace)
        return self._tables[namespace]

    def tank_readings(self) -> "TankReadingsTable":
        """Append-only time-series store for tank readings (one row/reading)."""
        return TankReadingsTable(self)

    # -- internals (used by the table views only) -----------------------------

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("Storage is not open — call open() first")
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, params)

    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            with self._connection:  # commit/rollback transaction
                return self._connection.execute(sql, params)

    def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Execute and consume a read while holding the connection lock."""
        with self._lock:
            return self._connection.execute(sql, params).fetchall()


class KeyValueTable(MutableMapping):
    """Dict-like view over one namespace in the ``kv`` table.

    Keys are strings; values are anything ``json.dumps`` accepts. Reads
    return fresh deserialized copies — mutating a returned value does NOT
    write back; assign it again to persist.
    """

    def __init__(self, storage: HubStorage, namespace: str) -> None:
        self._storage = storage
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    # -- MutableMapping interface ---------------------------------------------

    def __getitem__(self, key: str) -> Any:
        self._check_key(key)
        row = self._storage._execute(
            "SELECT value FROM kv WHERE namespace = ? AND key = ?",
            (self._namespace, key),
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key: str, value: Any) -> None:
        self._check_key(key)
        try:
            # allow_nan=False: a NaN/Infinity would serialize to a bare
            # NaN/Infinity token that strict JSON parsers (the app) reject,
            # poisoning the whole row — reject it at the write instead.
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as err:
            raise TypeError(
                f"Value for key {key!r} is not JSON-serializable: {err}"
            ) from err
        self._storage._execute_write(
            """
            INSERT INTO kv (namespace, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT (namespace, key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (self._namespace, key, payload),
        )

    def __delitem__(self, key: str) -> None:
        self._check_key(key)
        cursor = self._storage._execute_write(
            "DELETE FROM kv WHERE namespace = ? AND key = ?",
            (self._namespace, key),
        )
        if cursor.rowcount == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        rows = self._storage._execute(
            "SELECT key FROM kv WHERE namespace = ? ORDER BY key",
            (self._namespace,),
        ).fetchall()
        return iter(row[0] for row in rows)

    def __len__(self) -> int:
        row = self._storage._execute(
            "SELECT COUNT(*) FROM kv WHERE namespace = ?", (self._namespace,)
        ).fetchone()
        return int(row[0])

    def items(self) -> list[tuple[str, Any]]:
        """Return one consistent key/value snapshot.

        MutableMapping's default view iterates keys and then fetches each value
        separately. A concurrent delete between those operations raises a
        spurious KeyError in read paths such as tank/device listings.
        """
        rows = self._storage._fetchall(
            "SELECT key, value FROM kv WHERE namespace = ? ORDER BY key",
            (self._namespace,),
        )
        return [(key, json.loads(value)) for key, value in rows]

    def __contains__(self, key: object) -> bool:
        # MutableMapping's default __contains__ probes __getitem__, whose
        # _check_key raises TypeError for non-string keys. Membership is a
        # question, not a write — `None in table` is legitimate (optional
        # foreign ids like a room's floor_id) and the dict answer is False,
        # not a crash. Also saves fetching + JSON-parsing the whole value.
        if not isinstance(key, str) or not key:
            return False
        row = self._storage._execute(
            "SELECT 1 FROM kv WHERE namespace = ? AND key = ?",
            (self._namespace, key),
        ).fetchone()
        return row is not None

    def __repr__(self) -> str:
        return f"<KeyValueTable {self._namespace!r} ({len(self)} keys)>"

    # -- extras ----------------------------------------------------------------

    def clear(self) -> None:
        """Delete every key in this namespace (single statement, not per-key)."""
        self._storage._execute_write(
            "DELETE FROM kv WHERE namespace = ?", (self._namespace,)
        )

    def updated_at(self, key: str) -> str:
        """Return the ISO-8601 UTC timestamp of the key's last write."""
        self._check_key(key)
        row = self._storage._execute(
            "SELECT updated_at FROM kv WHERE namespace = ? AND key = ?",
            (self._namespace, key),
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]

    @staticmethod
    def _check_key(key: Any) -> None:
        if not isinstance(key, str) or not key:
            raise TypeError(f"Keys must be non-empty strings, got {key!r}")


class TankReadingsTable:
    """Append-only per-row store for tank readings (the ``tank_readings`` table).

    A high-frequency time series doesn't fit the rewrite-the-whole-value
    KeyValueTable shape — at a 5-minute cadence that re-serialized a ~270 KB
    blob every reading. Here an ingest is a single-row INSERT and retention is a
    bounded DELETE. Stays inside the storage layer so callers never touch SQL.
    """

    def __init__(self, storage: HubStorage) -> None:
        self._storage = storage

    def append(self, device_id: str, t: int, v: float) -> None:
        """Record one reading. (device_id, t) is unique — a repeated second
        replaces rather than doubles the row."""
        self._storage._execute_write(
            """
            INSERT INTO tank_readings (device_id, t, v) VALUES (?, ?, ?)
            ON CONFLICT (device_id, t) DO UPDATE SET v = excluded.v
            """,
            (device_id, int(t), float(v)),
        )

    def latest_t(self, device_id: str) -> int | None:
        """The newest reading's timestamp, or None — lets ingest keep t
        monotonic across a clock step-back (an NTP correction after reboot)."""
        row = self._storage._execute(
            "SELECT MAX(t) FROM tank_readings WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def last(self, device_id: str) -> dict[str, Any] | None:
        """The newest reading as ``{"t":..., "v":...}``, or None."""
        row = self._storage._execute(
            "SELECT t, v FROM tank_readings WHERE device_id = ? "
            "ORDER BY t DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        return {"t": int(row[0]), "v": float(row[1])} if row else None

    def recent(self, device_id: str, since_t: int) -> list[dict[str, Any]]:
        """Readings at/after ``since_t``, NEWEST first (the app's history shape).

        ORDER BY t makes 'newest first' robust to an out-of-order ingest t."""
        rows = self._storage._execute(
            "SELECT t, v FROM tank_readings WHERE device_id = ? AND t >= ? "
            "ORDER BY t DESC",
            (device_id, int(since_t)),
        ).fetchall()
        return [{"t": int(t), "v": float(v)} for t, v in rows]

    def prune(self, device_id: str, before_t: int) -> int:
        """Drop readings older than ``before_t`` (age-based retention); returns
        the count removed. Cheap + indexed — usually 0-1 rows once steady."""
        cursor = self._storage._execute_write(
            "DELETE FROM tank_readings WHERE device_id = ? AND t < ?",
            (device_id, int(before_t)),
        )
        return cursor.rowcount

    def delete_device(self, device_id: str) -> None:
        """Drop all readings for a device (when the device record is deleted)."""
        self._storage._execute_write(
            "DELETE FROM tank_readings WHERE device_id = ?", (device_id,)
        )
