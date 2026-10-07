"""JSON file store for rarely changed hub config (hub_config.json).

Holds hub identity, pairing and recovery code hashes, and network and relay
settings. Per-user or frequently written data belongs in HubStorage.

Writes go to a temp file that is fsynced and then renamed over the target.
The in-memory config only changes after that rename, so a failed write
changes nothing in memory or on disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
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
            _LOGGER.info("Config file %s missing; starting empty", self._path)
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
        """The stored value, or default when the key is unset."""
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

    def delete_many(self, keys: Iterable[str]) -> None:
        """Remove several keys with a single write; no write when none is set."""
        with self._lock:
            candidate = dict(self._data)
            removed = [k for k in keys if candidate.pop(k, _MISSING) is not _MISSING]
            if removed:
                self._save(candidate)
                self._data = candidate

    def as_dict(self) -> dict[str, Any]:
        """A shallow copy of the whole config."""
        with self._lock:
            return dict(self._data)

    # -- persistence -------------------------------------------------------------

    def _save(self, data: dict[str, Any]) -> None:
        """Atomically replace the file with data; ConfigError on failure."""
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as err:
            raise ConfigError(f"Config contains non-JSON value: {err}") from err

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=self._path.name, suffix=".tmp"
            )
        except OSError as err:
            raise ConfigError(f"Cannot write config {self._path}: {err}") from err
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

        # The rename is the commit point, so a failed directory fsync is only
        # logged. The fsync makes the rename survive a power cut.
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
