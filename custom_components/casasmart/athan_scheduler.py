"""Athan (prayer call) scheduler.

Computes the five daily prayer times offline with
prayer-times-calculator-offline (the library Home Assistant's Islamic Prayer
Times integration uses) from the config saved through PUT /audio/athan,
falling back to the hub's own location and timezone. At each prayer time it
publishes a play command with priority "athan" through the audio adapter:
one broadcast, or one command per selected speaker. The library works in
UTC, so daylight saving needs no special handling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, tzinfo
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)

from .audio import AudioError, normalize_mac6

_LOGGER = logging.getLogger(__name__)

# Prayer-call MP3s ship on each speaker's image at this path.
ATHAN_DIR = "/var/lib/speaker/athans"
PRAYER_NAMES = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")

# A prayer up to this many seconds past still plays (say, after a restart);
# anything older is skipped.
_GRACE_SEC = 120

# Calculation methods the library accepts. _METHOD_ALIASES maps other names
# the app may send; anything unknown uses _DEFAULT_METHOD.
_LIB_METHODS = frozenset(
    {
        "mwl",
        "isna",
        "egypt",
        "makkah",
        "karachi",
        "tehran",
        "jafari",
        "gulf",
        "kuwait",
        "qatar",
        "singapore",
        "france",
        "turkey",
        "russia",
        "moonsighting",
        "dubai",
        "jakim",
        "tunisia",
        "algeria",
        "kemenag",
        "morocco",
        "portugal",
        "jordan",
        "custom",
    }
)
_METHOD_ALIASES = {"egyptian": "egypt", "umalqura": "makkah", "umm_al_qura": "makkah"}
_DEFAULT_METHOD = "makkah"
_ASR_SCHOOLS = frozenset({"shafi", "hanafi"})


def compute_prayer_times_utc(
    lat: float, lon: float, method: str, school: str, date_str: str
) -> dict[str, datetime] | None:
    """The five prayer times on date_str, as aware UTC datetimes.

    Returns None if the library is missing or the calculation fails, so the
    caller schedules nothing.
    """
    try:
        from prayer_times_calculator_offline import PrayerTimesCalculator
    except Exception:
        _LOGGER.warning("Athan: prayer-times-calculator-offline not installed")
        return None

    m = _METHOD_ALIASES.get((method or "").lower(), (method or "").lower())
    if m not in _LIB_METHODS:
        m = _DEFAULT_METHOD
    sch = (school or "shafi").lower()
    if sch not in _ASR_SCHOOLS:
        sch = "shafi"

    try:
        calc = PrayerTimesCalculator(
            latitude=float(lat),
            longitude=float(lon),
            calculation_method=m,
            date=date_str,
            school=sch,
        )
        raw = calc.fetch_prayer_times()
    except Exception:
        _LOGGER.exception("Athan: prayer-time calculation failed")
        return None

    out: dict[str, datetime] = {}
    for prayer in PRAYER_NAMES:
        value = raw.get(prayer)
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:  # the library emits +00:00
            dt = dt.replace(tzinfo=UTC)
        out[prayer] = dt
    return out or None


class AthanScheduler:
    """Arms HA timers for today's prayers from the athan config."""

    def __init__(self, hass: HomeAssistant, engine: Any, adapter: Any) -> None:
        self._hass = hass
        self._engine = engine
        self._adapter = adapter
        self._unsub_prayers: list[Any] = []
        self._unsub_recompute: Any | None = None
        # Last computed schedule, served by GET /audio/athan.
        self._schedule: dict[str, Any] = {"enabled": False}
        # (date, prayer) pairs already played. A reschedule arms prayers up
        # to _GRACE_SEC past, so this keeps one from playing twice.
        self._fired: set[tuple[str, str]] = set()
        # Reschedules await a timezone lookup; one at a time, so the newest
        # config arms last.
        self._reschedule_lock = asyncio.Lock()
        # Stops a reschedule still awaiting its timezone from arming after
        # unload.
        self._stopped = False

    async def async_start(self) -> None:
        """Arm today's prayers and an hourly recompute.

        The hourly run (at minute 1) moves to the new day after midnight and
        re-arms the prayers still ahead, so a lost timer (a sleeping host, a
        toggle racing setup) costs at most an hour.
        """
        self._unsub_recompute = async_track_time_change(
            self._hass, self._async_handle_recompute, minute=1, second=0
        )
        await self.async_reschedule()

    async def async_stop(self) -> None:
        """Cancel all timers (idempotent)."""
        self._stopped = True
        self._cancel_prayers()
        if self._unsub_recompute is not None:
            self._unsub_recompute()
            self._unsub_recompute = None

    async def _async_handle_recompute(self, _now: datetime) -> None:
        """The hourly recompute."""
        await self.async_reschedule()

    def schedule_snapshot(self) -> dict[str, Any]:
        """The last computed schedule, for GET /audio/athan."""
        return dict(self._schedule)

    def _cancel_prayers(self) -> None:
        """Cancel the prayer timers."""
        for unsub in self._unsub_prayers:
            unsub()
        self._unsub_prayers = []

    async def async_reschedule(self) -> None:
        """Recompute today's times and arm timers for the prayers still ahead.

        Safe to call at any time. "Ahead" means not yet played today and at
        most _GRACE_SEC past. All timers are cleared when athan is off or no
        location or timezone resolves. The timezone is loaded with HA's async
        helper because a zone HA hasn't loaded yet is read from disk.
        """
        async with self._reschedule_lock:
            resolved = self._resolve_config()
            tz: tzinfo | None = None
            if resolved is not None:
                try:
                    tz = await dt_util.async_get_time_zone(resolved[2])
                except ValueError:
                    # zoneinfo raises for a malformed key such as "../x", and
                    # this runs during setup.
                    tz = None
            if not self._stopped:
                self._arm(resolved, tz)

    @callback
    def _arm(
        self,
        resolved: tuple[float, float, str, str, str] | None,
        tz: tzinfo | None,
    ) -> None:
        """Replace the timers with today's, for the resolved config in tz."""
        self._cancel_prayers()
        if resolved is None:
            _LOGGER.debug("Athan: disabled or no location — nothing scheduled")
            self._schedule = {"enabled": False}
            return
        lat, lon, tz_name, method, school = resolved

        if tz is None:
            _LOGGER.warning("Athan: unknown timezone %r — nothing scheduled", tz_name)
            self._schedule = {"enabled": True, "error": f"unknown timezone {tz_name!r}"}
            return

        today = datetime.now(tz).date()
        day = today.isoformat()
        self._fired = {key for key in self._fired if key[0] == day}
        times = compute_prayer_times_utc(lat, lon, method, school, day)
        if not times:
            _LOGGER.warning("Athan: could not compute prayer times for %s", today)
            self._schedule = {
                "enabled": True,
                "error": "prayer-time computation failed",
            }
            return

        athan = self._engine.get_athan() or {}
        has_sel, targets = self._resolve_targets(athan)

        now_utc = dt_util.utcnow()
        armed: list[str] = []
        prayers_out: list[dict[str, Any]] = []
        next_prayer: dict[str, Any] | None = None
        for prayer in PRAYER_NAMES:
            fire_at = times.get(prayer)
            if fire_at is None:
                continue
            local = fire_at.astimezone(tz).strftime("%H:%M")
            in_grace = (fire_at - now_utc).total_seconds() >= -_GRACE_SEC
            upcoming = in_grace and (day, prayer) not in self._fired
            prayers_out.append(
                {
                    "name": prayer,
                    "at": fire_at.isoformat(),
                    "local": local,
                    "upcoming": upcoming,
                }
            )
            if not upcoming:
                continue  # already played, or past the grace period
            unsub = async_track_point_in_time(
                self._hass, self._make_fire(prayer, day), fire_at
            )
            self._unsub_prayers.append(unsub)
            armed.append(f"{prayer} {local}")
            if next_prayer is None:
                next_prayer = {
                    "name": prayer,
                    "at": fire_at.isoformat(),
                    "local": local,
                }

        mode = "all" if not has_sel else ("subset" if targets else "none")
        self._schedule = {
            "enabled": True,
            "date": today.isoformat(),
            "timezone": tz_name,
            "method": method,
            "school": school,
            "speakers_mode": mode,
            "speakers": targets,
            "prayers": prayers_out,
            "next": next_prayer,
        }

        # A selection with no enrolled speakers means athan never plays. Warn,
        # so it shows even when HA logs only warnings.
        if has_sel and not targets:
            _LOGGER.warning(
                "Athan enabled for %s but its selected speakers are all un-enrolled — "
                "athan will fire on NO speakers until the selection is fixed.",
                today,
            )

        # The home's coordinates stay out of INFO, since logs get pasted into
        # public issues.
        _LOGGER.debug("Athan location for %s: %.4f,%.4f", today, lat, lon)
        _LOGGER.info(
            "Athan scheduled for %s (%s/%s, %s) on %s: %s",
            today,
            method,
            school,
            tz_name,
            "all speakers"
            if not has_sel
            else (
                f"{len(targets)} speaker(s): {','.join(targets)}"
                if targets
                else "NO speakers (empty selection)"
            ),
            ", ".join(armed) if armed else "none remaining today",
        )

    def _resolve_targets(self, athan: dict[str, Any]) -> tuple[bool, list[str]]:
        """Return (has_selection, target mac6s).

        No selection means every speaker. A selection is matched against the
        enrolled speakers, so removed ones drop out; unlike PA, it never falls
        back to a broadcast.
        """
        raw = athan.get("speakers")
        if not raw or not isinstance(raw, list):
            return False, []
        try:
            enrolled = {s.get("mac6") for s in self._engine.speakers()}
        except Exception:
            enrolled = set()
        targets: list[str] = []
        for item in raw:
            try:
                mac6 = normalize_mac6(item)
            except AudioError:
                continue
            if mac6 in enrolled and mac6 not in targets:
                targets.append(mac6)
        return True, targets

    def _make_fire(self, prayer: str, day: str) -> Any:
        """The timer callback for one prayer on day; records it as played."""

        @callback
        def _fire(_now: datetime) -> None:
            self._fired.add((day, prayer))
            self._fire_athan(prayer)

        return _fire

    def _fire_athan(self, prayer: str) -> None:
        """Publish the athan play for prayer to its target speakers.

        A speaker the bus can't reach right now is logged and skipped: the
        adapter refuses rather than queues, so an athan never plays late.
        """
        # Athan may have been turned off since the timer was armed.
        if self._resolve_config() is None:
            _LOGGER.info(
                "Athan: %s reached but athan is now disabled — skipping", prayer
            )
            return
        athan = self._engine.get_athan() or {}
        has_sel, targets = self._resolve_targets(athan)
        file_path = f"{ATHAN_DIR}/{prayer.lower()}.mp3"

        if has_sel and not targets:
            _LOGGER.warning(
                "Athan: %s — selected speakers are all un-enrolled; firing nowhere",
                prayer,
            )
            return

        # mac=None is a broadcast; a selection gets one play per speaker.
        macs: list[str | None] = targets if has_sel else [None]
        delivered = 0
        for mac in macs:
            try:
                topic, payload = self._engine.build_play(
                    mac=mac, file=file_path, priority="athan"
                )
            except Exception:
                _LOGGER.exception("Athan: failed to build %s play command", prayer)
                continue
            try:
                self._adapter.publish(topic, payload, qos=1)
                delivered += 1
            except Exception:
                _LOGGER.warning(
                    "Athan: %s not delivered to %s — MQTT bus unavailable",
                    prayer,
                    topic,
                )
        if delivered:
            _LOGGER.info(
                "Athan fired: %s -> %s",
                prayer,
                "all speakers"
                if not has_sel
                else f"{delivered} speaker(s): {','.join(targets)}",
            )

    def _resolve_config(
        self,
    ) -> tuple[float, float, str, str, str] | None:
        """Return (lat, lon, tz_name, method, school), or None if athan is off.

        Each coordinate and the timezone fall back to the hub's HA config, so
        athan works without a location set in the app.
        """
        try:
            athan = self._engine.get_athan()
        except Exception:
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
        method = athan.get("method") or _DEFAULT_METHOD
        school = athan.get("school") or "shafi"
        try:
            return float(lat), float(lon), str(tz_name), str(method), str(school)
        except (TypeError, ValueError):
            return None
