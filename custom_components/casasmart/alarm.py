"""Alarm engine: the arm state machine, sensor zones and event history.

The alarm runs on the hub, so it works with the app closed, and its state
survives a reboot. This module is stdlib only, with no Home Assistant
imports: alarm_adapter.py feeds it sensor edges and timer ticks, and
alarm_control_panel.py shows it in HA. Life-safety sensors (smoke, gas, CO,
leak) trigger in every mode, disarmed included.

Storage-touching methods are synchronous (call them via the executor) and
guarded by an RLock. Arm state and zones are also kept in memory, so
evaluating a sensor edge doesn't read storage.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

# -- Arm modes (the persisted state machine) -----------------------------------
MODE_DISARMED = "disarmed"
MODE_AWAY = "armed_away"
MODE_HOME = "armed_home"
MODE_NIGHT = "armed_night"
MODE_PENDING = "pending"  # entry delay running; triggers unless disarmed
MODE_TRIGGERED = "triggered"

# Modes the app can arm to. Only the engine enters pending and triggered.
ARMABLE_MODES = (MODE_AWAY, MODE_HOME, MODE_NIGHT)
ALL_MODES = (MODE_DISARMED, *ARMABLE_MODES, MODE_PENDING, MODE_TRIGGERED)

# -- Zones (which sensors fire in which mode) ----------------------------------
ZONE_PERIMETER = "perimeter"  # doors / windows
ZONE_INTERIOR = "interior"  # motion
ZONE_ENTRY = "entry"  # entry doors, which get the entry delay
ZONE_LIFE_SAFETY = "life_safety"  # smoke / gas / CO / leak; armed in every mode
ALL_ZONES = (ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY, ZONE_LIFE_SAFETY)

# Zones that can trigger in each mode (life safety is separate). The pending
# and triggered entries only feed the snapshot's active_zones.
_ACTIVE_ZONES_BY_MODE: dict[str, frozenset[str]] = {
    MODE_AWAY: frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
    MODE_HOME: frozenset({ZONE_PERIMETER}),
    MODE_NIGHT: frozenset({ZONE_PERIMETER, ZONE_ENTRY}),
    MODE_DISARMED: frozenset(),
    MODE_PENDING: frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
    MODE_TRIGGERED: frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
}

# -- Event kinds (alarm history + alert payloads) ------------------------------
EVENT_ARMED = "armed"
EVENT_DISARMED = "disarmed"
EVENT_ENTRY_DELAY = "entry_delay"  # entry sensor opened, countdown started
EVENT_TRIGGERED = "triggered"  # alarm went off (an armed zone or life safety)
EVENT_TAMPER = "tamper"  # sensor dropped offline while armed
EVENT_LIFE_SAFETY = "life_safety"  # smoke/gas/CO/leak, in any mode

# -- Defaults / bounds ---------------------------------------------------------
DEFAULT_ENTRY_DELAY_SECONDS = 30
DEFAULT_EXIT_DELAY_SECONDS = 60
_MAX_DELAY_SECONDS = 600  # upper bound for entry and exit delays
_NAME_MAX = 64
# The history is one stored blob, capped by event count and age.
_MAX_HISTORY = 1000
_HISTORY_RETENTION_SECONDS = 90 * 24 * 3600

# Storage keys.
_STATE_KEY = "current"  # single-row arm-state blob
_HISTORY_KEY = "events"  # single-row {"entries": [...]} blob
_SETTINGS_KEY = "defaults"  # single-row {"entry_delay", "exit_delay"} blob


class AlarmError(Exception):
    """Alarm input rejected (maps to HTTP 400)."""


class UnknownZoneError(AlarmError):
    """No sensor assigned under that entity id (maps to HTTP 404)."""


def _clean_name(name: Any) -> str:
    """A required, stripped, length-capped sensor display name."""
    if not isinstance(name, str) or not name.strip():
        raise AlarmError("Sensor name is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise AlarmError(f"Sensor name is too long (max {_NAME_MAX})")
    return cleaned


def _validate_zone(zone: Any) -> str:
    """One of ALL_ZONES, else AlarmError."""
    if zone not in ALL_ZONES:
        raise AlarmError(f"Unknown zone {zone!r} (expected one of {ALL_ZONES})")
    return zone


def _validate_delay(value: Any, *, field: str, default: int) -> int:
    """Whole seconds from 0 to _MAX_DELAY_SECONDS; None means the default."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AlarmError(f"{field} must be a non-negative integer seconds")
    if value > _MAX_DELAY_SECONDS:
        raise AlarmError(f"{field} must be <= {_MAX_DELAY_SECONDS} seconds")
    return value


def active_zones_for_mode(mode: str) -> frozenset[str]:
    """Zones that can trigger in mode, leaving out the always-armed life safety."""
    return _ACTIVE_ZONES_BY_MODE.get(mode, frozenset())


def _actor_str(actor: Any) -> str | None:
    """Who armed/disarmed, as a capped string for the history (or None)."""
    if actor is None:
        return None
    return str(actor)[:_NAME_MAX]


class AlarmEngine:
    """Arm state machine and zone model over four key-value tables.

    state_table holds the arm state, zones_table a {zone, name} row per
    sensor, history_table the event history and settings_table the default
    delays. alert_sink gets each trigger, life-safety and tamper event and by
    default logs a warning; phone push doesn't use it (the adapter fires
    EVENT_ALARM_TRIGGERED instead). clock is injectable for tests.
    """

    def __init__(
        self,
        state_table: Any,
        zones_table: Any,
        history_table: Any,
        settings_table: Any,
        *,
        alert_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_table = state_table
        self._zones_table = zones_table
        self._history_table = history_table
        self._settings_table = settings_table
        self._alert_sink = alert_sink or self._default_alert_sink
        self._clock = clock
        # Held across storage I/O.
        self._lock = threading.RLock()
        # In-memory copies, so sensor edges don't read storage.
        self._state: dict[str, Any] = self._default_state()
        self._zones: dict[str, dict[str, Any]] = {}
        # Kept on the hub so every phone arms with the same default delays.
        self._settings: dict[str, Any] = self._default_settings()

    # -- lifecycle -------------------------------------------------------------

    def warm_up(self) -> None:
        """Load the stored state, settings and zones into memory (blocking)."""
        with self._lock:
            stored = self._state_table.get(_STATE_KEY)
            self._state = self._coerce_state(stored)
            self._settings = self._coerce_settings(
                self._settings_table.get(_SETTINGS_KEY)
            )
            self._zones = {
                entity_id: dict(record)
                for entity_id, record in self._zones_table.items()
            }
            # The entry-delay timer died with the old process, so a restored
            # pending alarm fails secure to triggered.
            if self._state["mode"] == MODE_PENDING:
                _LOGGER.warning(
                    "Alarm restored from disk mid entry-delay — failing secure to triggered"
                )
                self._enter_triggered(
                    self._state.get("trigger_entity"),
                    self._state.get("trigger_zone", ZONE_ENTRY),
                    now=self._clock(),
                    persist=True,
                )

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "mode": MODE_DISARMED,
            "since": 0.0,
            "active_at": 0.0,  # exit-delay grace: triggers ignored before this
            "trigger_deadline": 0.0,  # entry-delay: pending -> triggered at this
            "armed_mode": None,  # the armed mode an entry delay interrupted
            "trigger_entity": None,
            "trigger_zone": None,
            "entry_delay": DEFAULT_ENTRY_DELAY_SECONDS,
        }

    def _coerce_state(self, stored: Any) -> dict[str, Any]:
        """Merge a persisted blob onto defaults, dropping anything invalid."""
        state = self._default_state()
        if isinstance(stored, dict):
            if stored.get("mode") in ALL_MODES:
                state["mode"] = stored["mode"]
            for key in (
                "since",
                "active_at",
                "trigger_deadline",
                "entry_delay",
            ):
                value = stored.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    state[key] = value
            if stored.get("armed_mode") in ARMABLE_MODES:
                state["armed_mode"] = stored["armed_mode"]
            for key in ("trigger_entity", "trigger_zone"):
                if isinstance(stored.get(key), str):
                    state[key] = stored[key]
        return state

    @staticmethod
    def _default_settings() -> dict[str, Any]:
        return {
            "entry_delay": DEFAULT_ENTRY_DELAY_SECONDS,
            "exit_delay": DEFAULT_EXIT_DELAY_SECONDS,
        }

    def _coerce_settings(self, stored: Any) -> dict[str, Any]:
        """Merge a persisted settings blob onto defaults, dropping bad values."""
        settings = self._default_settings()
        if isinstance(stored, dict):
            for key in ("entry_delay", "exit_delay"):
                value = stored.get(key)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value <= _MAX_DELAY_SECONDS
                ):
                    settings[key] = value
        return settings

    # -- default delays (storage; call via the executor) -----------------------

    def get_settings(self) -> dict[str, Any]:
        """The default entry and exit delays (a copy)."""
        with self._lock:
            return dict(self._settings)

    def set_settings(
        self, *, entry_delay: Any = None, exit_delay: Any = None
    ) -> dict[str, Any]:
        """Update either default delay and return the full settings.

        Omitted delays keep their value. Same rules as arm(): whole seconds,
        0-600.
        """
        with self._lock:
            updated = dict(self._settings)
            if entry_delay is not None:
                updated["entry_delay"] = _validate_delay(
                    entry_delay, field="entry_delay", default=updated["entry_delay"]
                )
            if exit_delay is not None:
                updated["exit_delay"] = _validate_delay(
                    exit_delay, field="exit_delay", default=updated["exit_delay"]
                )
            self._settings = updated
            self._settings_table[_SETTINGS_KEY] = dict(updated)
            return dict(updated)

    # -- zones (storage; call via the executor) --------------------------------

    def set_zone(self, entity_id: Any, zone: Any, name: Any = None) -> dict[str, Any]:
        """Assign entity_id to zone, replacing any assignment; return the record."""
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise AlarmError("entity_id is required")
        entity_id = entity_id.strip()
        zone = _validate_zone(zone)
        record = {"zone": zone, "name": _clean_name(name) if name else entity_id}
        with self._lock:
            self._zones_table[entity_id] = record
            self._zones[entity_id] = dict(record)
        return dict(record)

    def remove_zone(self, entity_id: Any) -> None:
        """Unassign a sensor; UnknownZoneError if it has no zone."""
        with self._lock:
            if entity_id not in self._zones:
                raise UnknownZoneError(f"No sensor assigned under {entity_id!r}")
            del self._zones_table[entity_id]
            self._zones.pop(entity_id, None)

    def zones(self) -> dict[str, dict[str, Any]]:
        """The entity_id -> {zone, name} map (a copy)."""
        with self._lock:
            return {eid: dict(rec) for eid, rec in self._zones.items()}

    def zone_of(self, entity_id: str) -> str | None:
        """The sensor's zone, or None if the alarm doesn't watch it.

        Lock-free, because the adapter calls it on every HA state change.
        """
        rec = self._zones.get(entity_id)
        return rec["zone"] if rec else None

    # -- arm / disarm (storage; call via the executor) -------------------------

    def arm(
        self,
        mode: Any,
        *,
        actor: Any = None,
        exit_delay: Any = None,
        entry_delay: Any = None,
    ) -> dict[str, Any]:
        """Arm to away, home or night from any state; return the snapshot.

        Sensor edges are ignored for exit_delay seconds so people can leave
        (life safety still fires); entry_delay is the countdown an entry-zone
        trip starts. Both default to the stored settings and must be whole
        seconds, 0-600.
        """
        if mode not in ARMABLE_MODES:
            raise AlarmError(
                f"Cannot arm to {mode!r} (expected one of {ARMABLE_MODES})"
            )
        with self._lock:
            default_exit = self._settings["exit_delay"]
            default_entry = self._settings["entry_delay"]
        exit_seconds = _validate_delay(
            exit_delay, field="exit_delay", default=default_exit
        )
        entry_seconds = _validate_delay(
            entry_delay, field="entry_delay", default=default_entry
        )
        now = self._clock()
        with self._lock:
            self._state.update(
                mode=mode,
                since=now,
                active_at=now + exit_seconds,
                trigger_deadline=0.0,
                armed_mode=None,
                trigger_entity=None,
                trigger_zone=None,
                entry_delay=entry_seconds,
            )
            self._persist_state()
            self._record_event(EVENT_ARMED, now=now, mode=mode, actor=_actor_str(actor))
        return self.snapshot()

    def disarm(self, *, actor: Any = None) -> dict[str, Any]:
        """Disarm, cancelling any entry delay and silencing a triggered alarm."""
        now = self._clock()
        with self._lock:
            was = self._state["mode"]
            self._state.update(
                mode=MODE_DISARMED,
                since=now,
                active_at=0.0,
                trigger_deadline=0.0,
                armed_mode=None,
                trigger_entity=None,
                trigger_zone=None,
            )
            self._persist_state()
            self._record_event(
                EVENT_DISARMED, now=now, from_mode=was, actor=_actor_str(actor)
            )
        return self.snapshot()

    # -- sensor edges (hot path; a no-op edge writes nothing) ------------------

    def process_sensor(
        self, entity_id: str, active: bool, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Evaluate one sensor edge and return the event it caused, if any.

        - An active life-safety sensor triggers at once, in any mode.
        - A sensor clearing never triggers.
        - Edges during the exit delay are ignored.
        - In an armed mode, an entry-zone trip starts the entry delay and any
          other active zone triggers at once.
        - During the entry delay other edges change nothing; the alarm
          triggers when it ends unless disarmed.
        - Zones not active in the current mode are ignored.
        """
        now = self._clock() if now is None else now
        record = self._zones.get(entity_id)
        if record is None:
            return None  # not an alarm sensor
        zone = record["zone"]

        # Life safety comes before any mode rule.
        if zone == ZONE_LIFE_SAFETY:
            if not active:
                return None
            with self._lock:
                return self._enter_triggered(
                    entity_id, zone, now=now, life_safety=True, persist=True
                )

        if not active:
            return None

        with self._lock:
            mode = self._state["mode"]
            if mode in (MODE_DISARMED, MODE_TRIGGERED):
                return None
            if mode == MODE_PENDING:
                return None
            # Exit delay still running.
            if now < self._state["active_at"]:
                return None
            if zone not in active_zones_for_mode(mode):
                return None

            if zone == ZONE_ENTRY:
                return self._enter_pending(entity_id, now=now)
            return self._enter_triggered(entity_id, zone, now=now, persist=True)

    def process_sensor_offline(
        self, entity_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """A mapped sensor went offline: tamper while armed, ignored otherwise.

        Tamper is recorded and passed to the alert sink but doesn't trigger
        the alarm or a phone push; a dead battery shouldn't wake the house.
        """
        now = self._clock() if now is None else now
        record = self._zones.get(entity_id)
        if record is None:
            return None
        with self._lock:
            if self._state["mode"] == MODE_DISARMED:
                return None
            event = self._record_event(
                EVENT_TAMPER, now=now, entity_id=entity_id, zone=record["zone"]
            )
        self._emit_alert(event)
        return event

    def tick(self, *, now: float | None = None) -> dict[str, Any] | None:
        """Trigger if the entry delay has run out; otherwise do nothing.

        The adapter calls this when its entry-delay timer fires.
        """
        now = self._clock() if now is None else now
        with self._lock:
            if self._state["mode"] != MODE_PENDING:
                return None
            if now < self._state["trigger_deadline"]:
                return None
            return self._enter_triggered(
                self._state.get("trigger_entity"),
                self._state.get("trigger_zone", ZONE_ENTRY),
                now=now,
                persist=True,
            )

    def pending_deadline(self) -> float | None:
        """When the running entry delay ends, or None.

        Lets the adapter set one timer instead of polling.
        """
        with self._lock:
            if self._state["mode"] != MODE_PENDING:
                return None
            return self._state["trigger_deadline"]

    # -- internal transitions (caller holds the lock) --------------------------

    def _enter_pending(self, entity_id: str, *, now: float) -> dict[str, Any]:
        """Start the entry-delay countdown for an entry-zone trip."""
        prior = self._state["mode"]
        deadline = now + self._state["entry_delay"]
        self._state.update(
            mode=MODE_PENDING,
            since=now,
            armed_mode=prior if prior in ARMABLE_MODES else self._state["armed_mode"],
            trigger_deadline=deadline,
            trigger_entity=entity_id,
            trigger_zone=ZONE_ENTRY,
        )
        self._persist_state()
        event = self._record_event(
            EVENT_ENTRY_DELAY,
            now=now,
            entity_id=entity_id,
            zone=ZONE_ENTRY,
            deadline=deadline,
        )
        # No alert yet: there is still time to disarm.
        return event

    def _enter_triggered(
        self,
        entity_id: str | None,
        zone: str | None,
        *,
        now: float,
        life_safety: bool = False,
        persist: bool = False,
    ) -> dict[str, Any]:
        """Go to triggered, record the event and hand it to the alert sink."""
        self._state.update(
            mode=MODE_TRIGGERED,
            since=now,
            trigger_deadline=0.0,
            trigger_entity=entity_id,
            trigger_zone=zone,
        )
        if persist:
            self._persist_state()
        kind = EVENT_LIFE_SAFETY if life_safety else EVENT_TRIGGERED
        event = self._record_event(
            kind, now=now, entity_id=entity_id, zone=zone, life_safety=life_safety
        )
        self._emit_alert(event)
        return event

    # -- alerts ----------------------------------------------------------------

    def _emit_alert(self, event: dict[str, Any]) -> None:
        """Pass the event to the alert sink; a failing sink can't break the alarm."""
        try:
            self._alert_sink(dict(event))
        except Exception:
            _LOGGER.exception("Alarm alert sink raised; alarm state is unaffected")

    @staticmethod
    def _default_alert_sink(event: dict[str, Any]) -> None:
        # For tamper, this log line is the only alert.
        _LOGGER.warning("Alarm alert (%s): %s", event.get("kind"), event)

    # -- history (storage) -----------------------------------------------------

    def _record_event(self, kind: str, *, now: float, **fields: Any) -> dict[str, Any]:
        """Append one event to the bounded history and return it.

        None fields are left out. The caller holds the lock.
        """
        event = {"kind": kind, "at": now}
        event.update({k: v for k, v in fields.items() if v is not None})
        blob = self._history_table.get(_HISTORY_KEY) or {}
        entries = blob.get("entries") if isinstance(blob, dict) else None
        if not isinstance(entries, list):
            entries = []
        entries.append(event)
        cutoff = now - _HISTORY_RETENTION_SECONDS
        entries = [e for e in entries if e.get("at", 0) >= cutoff][-_MAX_HISTORY:]
        self._history_table[_HISTORY_KEY] = {"entries": entries}
        return event

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Most-recent-first event history (bounded read)."""
        if not isinstance(limit, int) or limit < 1:
            raise AlarmError("limit must be a positive integer")
        blob = self._history_table.get(_HISTORY_KEY) or {}
        entries = blob.get("entries") if isinstance(blob, dict) else []
        if not isinstance(entries, list):
            entries = []
        return list(reversed(entries))[:limit]

    # -- snapshot / persistence ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The arm state the app shows, without internal bookkeeping."""
        with self._lock:
            s = self._state
            return {
                "mode": s["mode"],
                "since": s["since"],
                "active_zones": sorted(active_zones_for_mode(s["mode"])),
                # When the exit delay ends (the app counts down to it); None
                # unless armed. A past value means the delay is over.
                "arming_until": s["active_at"] if s["mode"] in ARMABLE_MODES else None,
                "pending_until": s["trigger_deadline"] or None
                if s["mode"] == MODE_PENDING
                else None,
                "trigger_entity": s["trigger_entity"]
                if s["mode"] in (MODE_PENDING, MODE_TRIGGERED)
                else None,
            }

    def _persist_state(self) -> None:
        """Persist the arm state so a reboot restores it (caller holds the lock)."""
        self._state_table[_STATE_KEY] = {
            "mode": self._state["mode"],
            "since": self._state["since"],
            "active_at": self._state["active_at"],
            "trigger_deadline": self._state["trigger_deadline"],
            "armed_mode": self._state["armed_mode"],
            "trigger_entity": self._state["trigger_entity"],
            "trigger_zone": self._state["trigger_zone"],
            "entry_delay": self._state["entry_delay"],
        }
