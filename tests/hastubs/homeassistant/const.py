"""The STATE_* constants the modules under test import.

Superset of what the retired per-suite stubs provided: ``alarm_adapter``
imports STATE_ON/UNAVAILABLE/UNKNOWN, ``push_dispatcher`` imports
STATE_UNAVAILABLE/UNKNOWN, and the lock tests exercise locked/unlocked.
"""

STATE_ON = "on"
STATE_LOCKED = "locked"
STATE_UNLOCKED = "unlocked"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"
