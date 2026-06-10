"""Server-side failure throttle (Track B — B2): exponential backoff.

The plan's brute-force posture, one reusable piece: "Every secret-guessing
surface is throttled server-side... N failed attempts (e.g. 5) from a
source -> lock that surface for a growing window (1 min -> 5 -> 30 ->
1 hr). Counter is per-source + per-account, resets on success."

B1.6 shipped a flat 30-minute wall inside the auth engine; B2 extracts
the throttle so the pairing surface (keyed per source IP) and the login
surface (keyed per device id) share one audited implementation, and
upgrades the flat wall to the plan's escalating windows.

Pure stdlib, no HA imports — unit-testable like the rest of the auth
stack. All state is in-memory: a hub reboot clears it, which costs an
attacker their progress, not us. Thread-safe (callers hit this from
executor threads).
"""

from __future__ import annotations

import logging
import threading
import time

_LOGGER = logging.getLogger(__name__)

# Failures inside a window before the wall goes up.
MAX_FAILURES = 5
# Escalating lockout windows (plan: 1 min -> 5 -> 30 -> 1 hr). Repeat
# offenders stay at the last step.
LOCKOUT_STEPS = (60.0, 5 * 60.0, 30 * 60.0, 60 * 60.0)
# Keys are attacker-influenced (device ids, source IPs) — cap the table so
# spam can't grow hub memory unbounded (locked entries are kept).
MAX_ENTRIES = 1000


class ThrottledError(Exception):
    """Too many failures — locked out."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Too many failed attempts, retry in {int(retry_after)}s")
        self.retry_after = retry_after


class FailureThrottle:
    """Failure counting + escalating lockouts, keyed by caller-chosen string."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        # key -> {failures, locked_until, level}
        self._entries: dict[str, dict[str, float]] = {}

    def check(self, key: str) -> None:
        """Raise ThrottledError when the key is inside a lockout window."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            remaining = entry.get("locked_until", 0.0) - time.monotonic()
            if remaining > 0:
                raise ThrottledError(remaining)

    def record_failure(self, key: str) -> None:
        """Count one failure; raise the wall (and escalate it) at the limit."""
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
                # Plan: failed bursts must be visible, not silent.
                _LOGGER.warning(
                    "[%s] lockout for %r after %d failures (%.0f min, level %d)",
                    self._name,
                    key,
                    MAX_FAILURES,
                    lockout / 60,
                    step + 1,
                )

    def clear(self, key: str) -> None:
        """Success — failures AND escalation level reset (plan: resets on success)."""
        with self._lock:
            self._entries.pop(key, None)

    def _prune(self, making_room_for: str) -> None:
        """Keep the table bounded; drop unlocked counters first (lock held)."""
        if len(self._entries) < MAX_ENTRIES or making_room_for in self._entries:
            return
        now = time.monotonic()
        for key in [
            k
            for k, v in self._entries.items()
            if v.get("locked_until", 0.0) <= now
        ][: len(self._entries) - MAX_ENTRIES + 1]:
            del self._entries[key]
