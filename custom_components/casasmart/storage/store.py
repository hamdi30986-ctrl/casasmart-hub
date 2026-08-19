"""CasaSmart runtime component."""

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



_BUSY_TIMEOUT_MS = 5000








_WAL_AUTOCHECKPOINT_PAGES = 1


class HubStorage:
    """CasaSmart runtime component."""

    def __init__(self, db_path: Path, backup_dir: Path | None = None) -> None:
        self._db_path = Path(db_path)
        self._backup_dir = (
            Path(backup_dir) if backup_dir else self._db_path.parent / "backups"
        )
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._tables: dict[str, KeyValueTable] = {}



    def open(self, migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
        """CasaSmart runtime component."""
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






            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                f"PRAGMA wal_autocheckpoint = {_WAL_AUTOCHECKPOINT_PAGES}"
            )
        except Exception:
            conn.close()
            raise

        self._conn = conn
        _LOGGER.info(
            "Storage open: %s (schema v%d, journal=%s, wal_autocheckpoint=%d)",
            self._db_path,
            schema_version,
            journal_mode,
            _WAL_AUTOCHECKPOINT_PAGES,
        )

    def close(self) -> None:
        """CasaSmart runtime component."""
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



    def table(self, namespace: str) -> "KeyValueTable":
        """CasaSmart runtime component."""
        if not _VALID_NAMESPACE.match(namespace):
            raise ValueError(
                f"Invalid table namespace {namespace!r}: must match "
                "[a-z][a-z0-9_]{0,63}"
            )
        if namespace not in self._tables:
            self._tables[namespace] = KeyValueTable(self, namespace)
        return self._tables[namespace]

    def tank_readings(self) -> "TankReadingsTable":
        """CasaSmart runtime component."""
        return TankReadingsTable(self)

    def energy_events(self) -> "EnergyEventsTable":
        """CasaSmart runtime component."""
        return EnergyEventsTable(self)



    @property
    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("Storage is not open — call open() first")
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, params)

    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._connection:
            return self._connection.execute(sql, params)

    def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        """CasaSmart runtime component."""
        with self._lock:
            return self._connection.execute(sql, params).fetchall()


class KeyValueTable(MutableMapping):
    """CasaSmart runtime component."""

    def __init__(self, storage: HubStorage, namespace: str) -> None:
        self._storage = storage
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace



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
        """CasaSmart runtime component."""
        rows = self._storage._fetchall(
            "SELECT key, value FROM kv WHERE namespace = ? ORDER BY key",
            (self._namespace,),
        )
        return [(key, json.loads(value)) for key, value in rows]

    def __contains__(self, key: object) -> bool:





        if not isinstance(key, str) or not key:
            return False
        row = self._storage._execute(
            "SELECT 1 FROM kv WHERE namespace = ? AND key = ?",
            (self._namespace, key),
        ).fetchone()
        return row is not None

    def __repr__(self) -> str:
        return f"<KeyValueTable {self._namespace!r} ({len(self)} keys)>"



    def clear(self) -> None:
        """CasaSmart runtime component."""
        self._storage._execute_write(
            "DELETE FROM kv WHERE namespace = ?", (self._namespace,)
        )

    def updated_at(self, key: str) -> str:
        """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""

    def __init__(self, storage: HubStorage) -> None:
        self._storage = storage

    def append(self, device_id: str, t: int, v: float) -> None:
        """CasaSmart runtime component."""
        self._storage._execute_write(
            """
            INSERT INTO tank_readings (device_id, t, v) VALUES (?, ?, ?)
            ON CONFLICT (device_id, t) DO UPDATE SET v = excluded.v
            """,
            (device_id, int(t), float(v)),
        )

    def latest_t(self, device_id: str) -> int | None:
        """CasaSmart runtime component."""
        row = self._storage._execute(
            "SELECT MAX(t) FROM tank_readings WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def last(self, device_id: str) -> dict[str, Any] | None:
        """CasaSmart runtime component."""
        row = self._storage._execute(
            "SELECT t, v FROM tank_readings WHERE device_id = ? "
            "ORDER BY t DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        return {"t": int(row[0]), "v": float(row[1])} if row else None

    def recent(self, device_id: str, since_t: int) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        rows = self._storage._execute(
            "SELECT t, v FROM tank_readings WHERE device_id = ? AND t >= ? "
            "ORDER BY t DESC",
            (device_id, int(since_t)),
        ).fetchall()
        return [{"t": int(t), "v": float(v)} for t, v in rows]

    def prune(self, device_id: str, before_t: int) -> int:
        """CasaSmart runtime component."""
        cursor = self._storage._execute_write(
            "DELETE FROM tank_readings WHERE device_id = ? AND t < ?",
            (device_id, int(before_t)),
        )
        return cursor.rowcount

    def delete_device(self, device_id: str) -> None:
        """CasaSmart runtime component."""
        self._storage._execute_write(
            "DELETE FROM tank_readings WHERE device_id = ?", (device_id,)
        )


class EnergyEventsTable:
    """CasaSmart runtime component."""

    _MAX_QUERY_LIMIT = 1000

    def __init__(self, storage: HubStorage) -> None:
        self._storage = storage

    def append(
        self,
        *,
        t: int,
        kind: str,
        level: str | None = None,
        entity_id: str | None = None,
        room_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        if isinstance(t, bool) or not isinstance(t, int) or t < 0:
            raise ValueError("t must be a non-negative integer timestamp")
        kind = self._required_text(kind, "kind", max_length=64)
        level = self._optional_text(level, "level", max_length=16)
        entity_id = self._optional_text(entity_id, "entity_id", max_length=255)
        room_id = self._optional_text(room_id, "room_id", max_length=255)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise TypeError("data must be a JSON object")
        try:
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as err:
            raise TypeError(f"event data is not JSON-serializable: {err}") from err

        cursor = self._storage._execute_write(
            """
            INSERT INTO energy_events
                (t, kind, level, entity_id, room_id, data)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (t, kind, level, entity_id, room_id, payload),
        )
        return {
            "id": int(cursor.lastrowid),
            "t": t,
            "kind": kind,
            "level": level,
            "entity_id": entity_id,
            "room_id": room_id,
            "data": json.loads(payload),
        }

    def recent(
        self,
        *,
        limit: int = 100,
        since_t: int | None = None,
        kinds: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self._MAX_QUERY_LIMIT
        ):
            raise ValueError(
                f"limit must be between 1 and {self._MAX_QUERY_LIMIT}"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if since_t is not None:
            if (
                isinstance(since_t, bool)
                or not isinstance(since_t, int)
                or since_t < 0
            ):
                raise ValueError("since_t must be a non-negative integer")
            clauses.append("t >= ?")
            params.append(since_t)
        if kinds is not None:
            if not isinstance(kinds, (list, tuple)) or not kinds:
                raise ValueError("kinds must be a non-empty list or tuple")
            clean_kinds = [
                self._required_text(kind, "kind", max_length=64)
                for kind in kinds
            ]
            clauses.append(f"kind IN ({','.join('?' for _ in clean_kinds)})")
            params.extend(clean_kinds)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._storage._fetchall(
            "SELECT id, t, kind, level, entity_id, room_id, data "
            f"FROM energy_events {where} "
            "ORDER BY t DESC, id DESC LIMIT ?",
            tuple(params),
        )
        return [self._row(row) for row in rows]

    def summary(self, *, since_t: int | None = None) -> dict[str, Any]:
        """CasaSmart runtime component."""
        params: tuple[Any, ...] = ()
        where = ""
        if since_t is not None:
            if (
                isinstance(since_t, bool)
                or not isinstance(since_t, int)
                or since_t < 0
            ):
                raise ValueError("since_t must be a non-negative integer")
            where = "WHERE t >= ?"
            params = (since_t,)
        kind_rows = self._storage._fetchall(
            f"SELECT kind, COUNT(*), MIN(t), MAX(t) "
            f"FROM energy_events {where} "
            "GROUP BY kind ORDER BY kind",
            params,
        )
        total = sum(int(row[1]) for row in kind_rows)
        first = min((int(row[2]) for row in kind_rows), default=None)
        last = max((int(row[3]) for row in kind_rows), default=None)
        return {
            "events_total": total,
            "first_event_at": first,
            "last_event_at": last,
            "event_counts": {
                kind: int(count) for kind, count, _first, _last in kind_rows
            },
        }

    def prune(self, *, before_t: int) -> int:
        """CasaSmart runtime component."""
        if (
            isinstance(before_t, bool)
            or not isinstance(before_t, int)
            or before_t < 0
        ):
            raise ValueError("before_t must be a non-negative integer")
        cursor = self._storage._execute_write(
            "DELETE FROM energy_events WHERE t < ?", (before_t,)
        )
        return cursor.rowcount

    def clear(self) -> None:
        """CasaSmart runtime component."""
        self._storage._execute_write("DELETE FROM energy_events")

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": int(row[0]),
            "t": int(row[1]),
            "kind": row[2],
            "level": row[3],
            "entity_id": row[4],
            "room_id": row[5],
            "data": json.loads(row[6]),
        }

    @staticmethod
    def _required_text(value: Any, field: str, *, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        clean = value.strip()
        if len(clean) > max_length:
            raise ValueError(f"{field} must be <= {max_length} characters")
        return clean

    @classmethod
    def _optional_text(
        cls, value: Any, field: str, *, max_length: int
    ) -> str | None:
        if value is None:
            return None
        return cls._required_text(value, field, max_length=max_length)
