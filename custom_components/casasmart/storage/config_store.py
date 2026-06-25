"""JsonConfigStore — JSON file for rarely-changed hub config.

Holds the small, near-static stuff: hub identity, network settings, version
pin. Multi-user / frequently-written data belongs in HubStorage (SQLite),
not here.

Writes are atomic: serialize to a temp file in the same directory, fsync,
then ``os.replace`` over the target — a crash mid-write can never leave a
half-written config behind.
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
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a key and persist immediately (config writes are rare)."""
        with self._lock:
            self._data[key] = value
            self._save()

    def delete(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, _MISSING) is not _MISSING:
                self._save()

    def update(self, values: dict[str, Any]) -> None:
        """Set several keys with a single write to disk."""
        with self._lock:
            self._data.update(values)
            self._save()

    def as_dict(self) -> dict[str, Any]:
        """Snapshot copy — mutating it does not touch the store."""
        with self._lock:
            return dict(self._data)

    # -- persistence -------------------------------------------------------------

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.dumps(self._data, indent=2, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as err:
            raise ConfigError(f"Config contains non-JSON value: {err}") from err

        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
            # fsync the directory so the rename itself is durable: without it a
            # power cut just after replace() can lose the rename — and with it
            # the permanent pairing/recovery code hashes this file holds.
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as err:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise ConfigError(f"Cannot write config {self._path}: {err}") from err
