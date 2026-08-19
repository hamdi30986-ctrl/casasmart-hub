"""CasaSmart runtime component."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)


MODE_DISARMED = "disarmed"
MODE_AWAY = "armed_away"
MODE_HOME = "armed_home"
MODE_NIGHT = "armed_night"
MODE_PENDING = "pending"
MODE_TRIGGERED = "triggered"



ARMABLE_MODES = (MODE_AWAY, MODE_HOME, MODE_NIGHT)
ALL_MODES = (MODE_DISARMED, *ARMABLE_MODES, MODE_PENDING, MODE_TRIGGERED)


ZONE_PERIMETER = "perimeter"
ZONE_INTERIOR = "interior"
ZONE_ENTRY = "entry"
ZONE_LIFE_SAFETY = "life_safety"
ALL_ZONES = (ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY, ZONE_LIFE_SAFETY)




_ACTIVE_ZONES_BY_MODE: dict[str, frozenset[str]] = {
    MODE_AWAY: frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
    MODE_HOME: frozenset({ZONE_PERIMETER}),
    MODE_NIGHT: frozenset({ZONE_PERIMETER, ZONE_ENTRY}),
    MODE_DISARMED: frozenset(),
    MODE_PENDING: frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
    MODE_TRIGGERED: frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
}


EVENT_ARMED = "armed"
EVENT_DISARMED = "disarmed"
EVENT_ENTRY_DELAY = "entry_delay"
EVENT_TRIGGERED = "triggered"
EVENT_TAMPER = "tamper"
EVENT_LIFE_SAFETY = "life_safety"


DEFAULT_ENTRY_DELAY_SECONDS = 30
DEFAULT_EXIT_DELAY_SECONDS = 60
_MAX_DELAY_SECONDS = 600
_NAME_MAX = 64


_MAX_HISTORY = 1000
_HISTORY_RETENTION_SECONDS = 90 * 24 * 3600


_STATE_KEY = "current"
_HISTORY_KEY = "events"
_SETTINGS_KEY = "defaults"


class AlarmError(Exception):
    """CasaSmart runtime component."""


class UnknownZoneError(AlarmError):
    """CasaSmart runtime component."""


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise AlarmError("Sensor name is required")
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise AlarmError(f"Sensor name is too long (max {_NAME_MAX})")
    return cleaned


def _validate_zone(zone: Any) -> str:
    if zone not in ALL_ZONES:
        raise AlarmError(f"Unknown zone {zone!r} (expected one of {ALL_ZONES})")
    return zone


def _validate_delay(value: Any, *, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AlarmError(f"{field} must be a non-negative integer seconds")
    if value > _MAX_DELAY_SECONDS:
        raise AlarmError(f"{field} must be <= {_MAX_DELAY_SECONDS} seconds")
    return value


def active_zones_for_mode(mode: str) -> frozenset[str]:
    """CasaSmart runtime component."""
    return _ACTIVE_ZONES_BY_MODE.get(mode, frozenset())


class AlarmEngine:
    """CasaSmart runtime component."""

    def __init__(
        self,
        state_table: Any,
        zones_table: Any,
        history_table: Any,
        settings_table: Any,
        *,
        alert_sink: Optional[Callable[[dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_table = state_table
        self._zones_table = zones_table
        self._history_table = history_table
        self._settings_table = settings_table
        self._alert_sink = alert_sink or self._default_alert_sink
        self._clock = clock

        self._lock = threading.RLock()

        self._state: dict[str, Any] = self._default_state()
        self._zones: dict[str, dict[str, Any]] = {}



        self._settings: dict[str, Any] = self._default_settings()



    def warm_up(self) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            stored = self._state_table.get(_STATE_KEY)
            self._state = self._coerce_state(stored)
            self._settings = self._coerce_settings(self._settings_table.get(_SETTINGS_KEY))
            self._zones = {
                entity_id: dict(record)
                for entity_id, record in self._zones_table.items()
            }




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
            "active_at": 0.0,
            "trigger_deadline": 0.0,
            "armed_mode": None,
            "trigger_entity": None,
            "trigger_zone": None,
            "entry_delay": DEFAULT_ENTRY_DELAY_SECONDS,
        }

    def _coerce_state(self, stored: Any) -> dict[str, Any]:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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



    def get_settings(self) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            return dict(self._settings)

    def set_settings(
        self, *, entry_delay: Any = None, exit_delay: Any = None
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
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



    def set_zone(self, entity_id: Any, zone: Any, name: Any = None) -> dict[str, Any]:
        """CasaSmart runtime component."""
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
        with self._lock:
            if entity_id not in self._zones:
                raise UnknownZoneError(f"No sensor assigned under {entity_id!r}")
            del self._zones_table[entity_id]
            self._zones.pop(entity_id, None)

    def zones(self) -> dict[str, dict[str, Any]]:
        """CasaSmart runtime component."""
        with self._lock:
            return {eid: dict(rec) for eid, rec in self._zones.items()}

    def zone_of(self, entity_id: str) -> Optional[str]:
        rec = self._zones.get(entity_id)
        return rec["zone"] if rec else None



    def arm(
        self,
        mode: Any,
        *,
        actor: Any = None,
        exit_delay: Any = None,
        entry_delay: Any = None,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
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
            self._record_event(
                EVENT_ARMED, now=now, mode=mode, actor=_actor_str(actor)
            )
        return self.snapshot()

    def disarm(self, *, actor: Any = None) -> dict[str, Any]:
        """CasaSmart runtime component."""
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




    def process_sensor(
        self, entity_id: str, active: bool, *, now: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        """CasaSmart runtime component."""
        now = self._clock() if now is None else now
        record = self._zones.get(entity_id)
        if record is None:
            return None
        zone = record["zone"]


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

            if now < self._state["active_at"]:
                return None
            if zone not in active_zones_for_mode(mode):
                return None

            if zone == ZONE_ENTRY:
                if mode == MODE_PENDING:
                    return None
                return self._enter_pending(entity_id, now=now)

            return self._enter_triggered(entity_id, zone, now=now, persist=True)

    def process_sensor_offline(
        self, entity_id: str, *, now: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        """CasaSmart runtime component."""
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

    def tick(self, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """CasaSmart runtime component."""
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

    def pending_deadline(self) -> Optional[float]:
        """CasaSmart runtime component."""
        with self._lock:
            if self._state["mode"] != MODE_PENDING:
                return None
            return self._state["trigger_deadline"]



    def _enter_pending(self, entity_id: str, *, now: float) -> dict[str, Any]:
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

        return event

    def _enter_triggered(
        self,
        entity_id: Optional[str],
        zone: Optional[str],
        *,
        now: float,
        life_safety: bool = False,
        persist: bool = False,
    ) -> dict[str, Any]:
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



    def _emit_alert(self, event: dict[str, Any]) -> None:
        """CasaSmart runtime component."""
        try:
            self._alert_sink(dict(event))
        except Exception:
            _LOGGER.exception("Alarm alert sink raised; alarm state is unaffected")

    @staticmethod
    def _default_alert_sink(event: dict[str, Any]) -> None:


        _LOGGER.warning("ALARM ALERT (push not yet wired — B8): %s", event)



    def _record_event(self, kind: str, *, now: float, **fields: Any) -> dict[str, Any]:
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
        """CasaSmart runtime component."""
        if not isinstance(limit, int) or limit < 1:
            raise AlarmError("limit must be a positive integer")
        blob = self._history_table.get(_HISTORY_KEY) or {}
        entries = blob.get("entries") if isinstance(blob, dict) else []
        if not isinstance(entries, list):
            entries = []
        return list(reversed(entries))[:limit]



    def snapshot(self) -> dict[str, Any]:
        """CasaSmart runtime component."""
        with self._lock:
            s = self._state
            return {
                "mode": s["mode"],
                "since": s["since"],
                "active_zones": sorted(active_zones_for_mode(s["mode"])),





                "arming_until": s["active_at"]
                if s["mode"] in ARMABLE_MODES
                else None,
                "pending_until": s["trigger_deadline"] or None
                if s["mode"] == MODE_PENDING
                else None,
                "trigger_entity": s["trigger_entity"]
                if s["mode"] in (MODE_PENDING, MODE_TRIGGERED)
                else None,
            }

    def _persist_state(self) -> None:
        """CasaSmart runtime component."""
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


def _actor_str(actor: Any) -> Optional[str]:
    if actor is None:
        return None
    return str(actor)[:_NAME_MAX]
