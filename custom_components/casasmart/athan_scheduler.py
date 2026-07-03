"""Hub-native athan (prayer-call) scheduler.

Computes the five daily prayer times locally — pure-Python solar algorithm, no
network and no external library — from the athan config the app stores via
``PUT /audio/athan`` (``lat``/``lon``/``timezone``/``method``), falling back to
the hub's own configured location (``hass.config.latitude/longitude/time_zone``).
It arms one HA timer per prayer and, at prayer time, publishes a broadcast
``play`` command (priority ``athan``) through the audio adapter — the same play
path PA uses.

This replaces the standalone ``casaos-athan-scheduler`` daemon. The hub owns
audio (B14), so it owns athan scheduling too. Being in-process means it ships
with every hub, resolves timezones through HA's DST-aware ``zoneinfo`` database
(so prayer times are correct in any region, not just no-DST Saudi Arabia), and
needs no Supabase, no hardcoded home id, and no separate broker credentials.

The prayer-time math is ported verbatim from the (verified-accurate) daemon;
only the timezone handling is upgraded from a fixed offset table to real,
per-date IANA offsets.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, tzinfo
from typing import Any, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)
import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Prayer-call MP3s are baked onto each Pi speaker image at this path.
ATHAN_DIR = "/var/lib/speaker/athans"
PRAYER_NAMES = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")

# If the hub was down/asleep across a prayer time, don't blast a stale athan on
# wake — skip anything already more than this many seconds past.
_GRACE_SEC = 120


# ── Pure prayer-time math (ported from the verified daemon) ───────────────────
# Standard solar-position algorithm; fajr/isha depression angles per method.

def _method_params(method: str) -> dict[str, Optional[float]]:
    m = (method or "makkah").lower()
    if m in ("makkah", "umalqura", "umm_al_qura"):
        return {"fajr": 18.5, "isha": None, "isha_min": 90}
    if m in ("mwl", "muslim_world_league"):
        return {"fajr": 18.0, "isha": 17.0, "isha_min": None}
    if m in ("egyptian",):
        return {"fajr": 19.5, "isha": 17.5, "isha_min": None}
    if m in ("karachi",):
        return {"fajr": 18.0, "isha": 18.0, "isha_min": None}
    if m in ("isna", "north_america"):
        return {"fajr": 15.0, "isha": 15.0, "isha_min": None}
    return {"fajr": 18.5, "isha": None, "isha_min": 90}  # default: makkah


def _julian_date(year: int, month: int, day: int) -> float:
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + b - 1524.5


def _sun_position(jd: float) -> tuple[float, float]:
    """Return (declination, equation-of-time) in degrees / hours."""
    d = jd - 2451545.0
    g = (357.529 + 0.98560028 * d) % 360
    q = (280.459 + 0.98564736 * d) % 360
    lam = (q + 1.915 * math.sin(math.radians(g)) + 0.020 * math.sin(math.radians(2 * g))) % 360
    e = 23.439 - 0.00000036 * d
    ra = math.degrees(
        math.atan2(
            math.cos(math.radians(e)) * math.sin(math.radians(lam)),
            math.cos(math.radians(lam)),
        )
    )
    decl = math.degrees(math.asin(math.sin(math.radians(e)) * math.sin(math.radians(lam))))
    eqt = (q / 15.0) - (ra / 15.0)
    while eqt > 12:
        eqt -= 24
    while eqt < -12:
        eqt += 24
    return decl, eqt


def _hour_angle(angle: float, lat: float, decl: float) -> float:
    cos_h = (
        -math.sin(math.radians(angle))
        - math.sin(math.radians(lat)) * math.sin(math.radians(decl))
    ) / (math.cos(math.radians(lat)) * math.cos(math.radians(decl)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_h)))) / 15.0


def _asr_angle(lat: float, decl: float) -> float:
    angle = math.atan(1.0 / (1 + math.tan(abs(math.radians(lat) - math.radians(decl)))))
    cos_h = (
        math.sin(angle) - math.sin(math.radians(lat)) * math.sin(math.radians(decl))
    ) / (math.cos(math.radians(lat)) * math.cos(math.radians(decl)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_h)))) / 15.0


def prayer_times_local(
    for_date: date, lat: float, lon: float, utc_offset: float, method: str
) -> dict[str, float]:
    """The five prayer times as hours-since-local-midnight (float, 0..24)."""
    p = _method_params(method)
    jd = _julian_date(for_date.year, for_date.month, for_date.day)
    decl, eqt = _sun_position(jd)
    dhuhr = 12.0 + utc_offset - (lon / 15.0) - eqt
    sunset = dhuhr + _hour_angle(0.833, lat, decl)
    isha = (
        sunset + p["isha_min"] / 60.0
        if p["isha_min"]
        else dhuhr + _hour_angle(p["isha"], lat, decl)
    )
    return {
        "Fajr": (dhuhr - _hour_angle(p["fajr"], lat, decl)) % 24,
        "Dhuhr": dhuhr % 24,
        "Asr": (dhuhr + _asr_angle(lat, decl)) % 24,
        "Maghrib": sunset % 24,
        "Isha": isha % 24,
    }


def _offset_hours(tz: tzinfo, for_date: date) -> float:
    """The tz's UTC offset (hours) at local noon on ``for_date`` — DST-aware.

    Sampling at noon keeps us safely inside the day, away from the ambiguous
    hour around a DST transition.
    """
    dt = datetime.combine(for_date, time(12, 0), tzinfo=tz)
    off = dt.utcoffset()
    return off.total_seconds() / 3600.0 if off is not None else 0.0


# ── Scheduler ─────────────────────────────────────────────────────────────────
class AthanScheduler:
    """Computes prayer times off the athan config and fires them on HA's clock."""

    def __init__(self, hass: HomeAssistant, engine: Any, adapter: Any) -> None:
        self._hass = hass
        self._engine = engine
        self._adapter = adapter
        self._unsub_prayers: list[Any] = []
        self._unsub_midnight: Optional[Any] = None

    async def async_start(self) -> None:
        """Arm today's prayers and a daily 00:01 recompute."""
        self._unsub_midnight = async_track_time_change(
            self._hass, self._handle_midnight, hour=0, minute=1, second=0
        )
        self.reschedule()

    async def async_stop(self) -> None:
        """Cancel every armed timer (idempotent)."""
        self._cancel_prayers()
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None

    @callback
    def _handle_midnight(self, _now: datetime) -> None:
        self.reschedule()

    def _cancel_prayers(self) -> None:
        for unsub in self._unsub_prayers:
            unsub()
        self._unsub_prayers = []

    @callback
    def reschedule(self, *_: Any) -> None:
        """(Re)compute today's times and arm timers for the prayers still ahead.

        Safe to call any time — from setup, the midnight tick, or a config PUT.
        A no-op (all timers cleared) when athan is disabled or no location is
        resolvable.
        """
        self._cancel_prayers()
        resolved = self._resolve_config()
        if resolved is None:
            _LOGGER.debug("Athan: disabled or no location — nothing scheduled")
            return
        lat, lon, tz_name, method = resolved

        tz = dt_util.get_time_zone(tz_name)
        if tz is None:
            _LOGGER.warning("Athan: unknown timezone %r — nothing scheduled", tz_name)
            return

        now = datetime.now(tz)
        today = now.date()
        offset = _offset_hours(tz, today)
        times = prayer_times_local(today, lat, lon, offset, method)

        armed: list[str] = []
        for prayer in PRAYER_NAMES:
            hours = times[prayer]
            # Floor to the minute — matches the original daemon so Jeddah users
            # see the exact prayer minutes they're already used to.
            h = int(hours)
            m = int((hours - h) * 60)
            fire_at = datetime.combine(today, time(h, m), tzinfo=tz)
            delay = (fire_at - now).total_seconds()
            if delay < -_GRACE_SEC:
                continue  # already well past — skip (grace guards a late wake)
            unsub = async_track_point_in_time(
                self._hass, self._make_fire(prayer), fire_at
            )
            self._unsub_prayers.append(unsub)
            armed.append(f"{prayer} {h:02d}:{m:02d}")

        _LOGGER.info(
            "Athan scheduled for %s (%s, %.4f,%.4f %s): %s",
            today, method, lat, lon, tz_name,
            ", ".join(armed) if armed else "none remaining today",
        )

    def _make_fire(self, prayer: str) -> Any:
        @callback
        def _fire(_now: datetime) -> None:
            self._fire_athan(prayer)
        return _fire

    def _fire_athan(self, prayer: str) -> None:
        # Re-check at fire time: the config may have been disabled since arming.
        if self._resolve_config() is None:
            _LOGGER.info("Athan: %s reached but athan is now disabled — skipping", prayer)
            return
        try:
            topic, payload = self._engine.build_play(
                file=f"{ATHAN_DIR}/{prayer.lower()}.mp3", priority="athan"
            )
        except Exception:  # noqa: BLE001 — a build error must not kill the loop
            _LOGGER.exception("Athan: failed to build %s play command", prayer)
            return
        try:
            self._adapter.publish(topic, payload, qos=1)
            _LOGGER.info("Athan fired: %s -> %s", prayer, topic)
        except Exception:  # noqa: BLE001 — a dead bus is non-fatal, just logged
            _LOGGER.warning("Athan: %s not delivered — MQTT bus unavailable", prayer)

    def _resolve_config(self) -> Optional[tuple[float, float, str, str]]:
        """Return ``(lat, lon, tz_name, method)`` or None if athan is off / no location.

        Location and timezone fall back to the hub's own HA config so a client
        hub configured with the customer's location works with no app-side setup.
        """
        try:
            athan = self._engine.get_athan()
        except Exception:  # noqa: BLE001
            return None
        if not athan or not athan.get("enabled"):
            return None
        lat = athan.get("lat")
        lon = athan.get("lon")
        if lat is None:
            lat = self._hass.config.latitude
        if lon is None:
            lon = self._hass.config.longitude
        if lat is None or lon is None:
            return None
        tz_name = athan.get("timezone") or self._hass.config.time_zone or "UTC"
        method = athan.get("method") or "makkah"
        try:
            return float(lat), float(lon), str(tz_name), str(method)
        except (TypeError, ValueError):
            return None
