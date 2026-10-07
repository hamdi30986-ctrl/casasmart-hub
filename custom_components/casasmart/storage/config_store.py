"""JsonConfigStore — JSON file for rarely changed hub config.

Holds the small, near-static settings and secrets in ``hub_config.json``: hub
identity, pairing and recovery code hashes, network and relay settings (the
README's "Hub settings"). Per-user or frequently written data belongs in
HubStorage (SQLite), not here.

Writes are atomic: serialize to a temp file in the same directory, fsync,
then ``os.replace`` over the target — a crash mid-write can never leave a
half-written config behind.

Writes are also copy-on-write: each change is made to a candidate copy, and the
in-memory config only becomes that copy once the replace has put it on disk. A
write that raises therefore changes nothing, in memory or on disk, and a
rejected value cannot ride along with a later, unrelated write.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .exceptions import ConfigError

_LOGGER = logging.getLogger(__name__)

_MISSING = object()


class JsonConfigStore:
    """Dict-like access to a single JSON config file with atomic writes."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """Read the file; a missing file is an empty config, a bad one an error."""
        if not self._path.exists():
            _LOGGER.info("Config file %s missing — starting empty", self._path)
            return {}
        try:
            with self._path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as err:
            raise ConfigError(f"Cannot read config {self._path}: {err}") from err
        if not isinstance(data, dict):
            raise ConfigError(
                f"Config {self._path} must contain a JSON object, "
                f"got {type(data).__name__}"
            )
        return data

    # -- access ----------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """The stored value, or ``default`` when the key is unset."""
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a key and persist immediately (config writes are rare)."""
        with self._lock:
            candidate = dict(self._data)
            candidate[key] = value
            self._save(candidate)
            self._data = candidate

    def delete(self, key: str) -> None:
        """Remove a key and persist; a no-op (no write) when it is unset."""
        with self._lock:
            candidate = dict(self._data)
            if candidate.pop(key, _MISSING) is not _MISSING:
                self._save(candidate)
                self._data = candidate

    def update(self, values: dict[str, Any]) -> None:
        """Set several keys with a single write to disk."""
        with self._lock:
            candidate = dict(self._data)
            candidate.update(values)
            self._save(candidate)
            self._data = candidate

    def as_dict(self) -> dict[str, Any]:
        """Snapshot copy — mutating it does not touch the store."""
        with self._lock:
            return dict(self._data)

    # -- persistence -------------------------------------------------------------

    def _save(self, data: dict[str, Any]) -> None:
        """Atomically replace the file with ``data``; ConfigError on failure."""
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as err:
            raise ConfigError(f"Config contains non-JSON value: {err}") from err

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except OSError as err:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise ConfigError(f"Cannot write config {self._path}: {err}") from err

        # The replace is the commit point: from here on every reader, and the
        # next start, sees the new config, so it is not reported as failed.
        # fsync the directory so the rename itself is durable: without it a
        # power cut just after replace() can lose the rename — and with it
        # the permanent pairing/recovery code hashes this file holds.
        try:
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as err:
            _LOGGER.warning(
                "Config %s was written, but syncing its directory failed (%s); "
                "a power cut now could undo the write",
                self._path,
                err,
            )
