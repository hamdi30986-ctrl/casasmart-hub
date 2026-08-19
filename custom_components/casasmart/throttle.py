"""CasaSmart runtime component."""

from __future__ import annotations

import logging
import threading
import time

_LOGGER = logging.getLogger(__name__)


MAX_FAILURES = 5


LOCKOUT_STEPS = (60.0, 5 * 60.0, 30 * 60.0, 60 * 60.0)


MAX_ENTRIES = 1000


class ThrottledError(Exception):
    """CasaSmart runtime component."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Too many failed attempts, retry in {int(retry_after)}s")
        self.retry_after = retry_after


class FailureThrottle:
    """CasaSmart runtime component."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()

        self._entries: dict[str, dict[str, float]] = {}

    def check(self, key: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            remaining = entry.get("locked_until", 0.0) - time.monotonic()
            if remaining > 0:
                raise ThrottledError(remaining)

    def record_failure(self, key: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            self._prune(making_room_for=key)
            entry = self._entries.setdefault(
                key, {"failures": 0.0, "locked_until": 0.0, "level": 0.0}
            )
            entry["failures"] += 1
            if entry["failures"] >= MAX_FAILURES:
                step = min(int(entry["level"]), len(LOCKOUT_STEPS) - 1)
                lockout = LOCKOUT_STEPS[step]
                entry["locked_until"] = time.monotonic() + lockout
                entry["failures"] = 0.0
                entry["level"] = min(step + 1, len(LOCKOUT_STEPS) - 1)

                _LOGGER.warning(
                    "[%s] lockout for %r after %d failures (%.0f min, level %d)",
                    self._name,
                    key,
                    MAX_FAILURES,
                    lockout / 60,
                    step + 1,
                )

    def clear(self, key: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            self._entries.pop(key, None)

    def _prune(self, making_room_for: str) -> None:
        """CasaSmart runtime component."""
        if len(self._entries) < MAX_ENTRIES or making_room_for in self._entries:
            return
        now = time.monotonic()
        for key in [
            k
            for k, v in self._entries.items()
            if v.get("locked_until", 0.0) <= now
        ][: len(self._entries) - MAX_ENTRIES + 1]:
            del self._entries[key]
