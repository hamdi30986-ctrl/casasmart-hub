"""Hub-native athan (prayer-call) scheduler.

Computes the five daily prayer times locally via
``prayer-times-calculator-offline`` (the same offline, no-network library Home
Assistant's *Islamic Prayer Times* integration uses) from the athan config the
app stores through ``PUT /audio/athan`` (``lat``/``lon``/``timezone``/
``method``/``school``), falling back to the hub's own configured location
(``hass.config.latitude/longitude/time_zone``). It arms one HA timer per
prayer and, at prayer time, publishes a ``play`` command (priority
``athan``) through the audio adapter — the same play path PA uses: one
broadcast, or one command per selected speaker when the config names some.

The hub owns audio, so it owns athan scheduling too — no separate scheduler
daemon. The library returns UTC timestamps, so DST/offset handling is inherent
(no fixed table), and it supports Hanafi/Shafi Asr, high-latitude rules and
~24 regional calculation methods — correct in any region, with no cloud
lookup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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

# If the hub was down/asleep across a prayer time, don't blast a stale athan on
# wake — skip anything already more than this many seconds past.
_GRACE_SEC = 120

# The library's accepted calculation methods (lower-case). The app may send
# "egyptian" (the library's "egypt") or "umalqura"/"umm_al_qura" (the
# library's "makkah"), so those are aliased. Anything unknown falls back to
# _DEFAULT_METHOD.
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
    """The five prayer times as timezone-aware UTC datetimes for ``date_str``.

    Uses ``prayer-times-calculator-offline`` (pure local math, no network).
    Returns None if the library is missing or the calculation fails, so the
    caller schedules nothing rather than crashing the loop.
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
        if dt.tzinfo is None:  # library emits +00:00, but be defensive
            dt = dt.replace(tzinfo=UTC)
        out[prayer] = dt
    return out or None


class AthanScheduler:
    """Computes prayer times off the athan config and fires them on HA's clock."""

    def __init__(self, hass: HomeAssistant, engine: Any, adapter: Any) -> None:
        self._hass = hass
        self._engine = engine
        self._adapter = adapter
        self._unsub_prayers: list[Any] = []
        self._unsub_recompute: Any | None = None
        # Last computed schedule, for the GET /audio/athan `schedule` block so
        # the app can show "next athan" and a silent failure can't hide.
        self._schedule: dict[str, Any] = {"enabled": False}
        # (date, prayer) pairs whose timer already fired. reschedule() arms
        # anything up to _GRACE_SEC past, so without this the hourly re-arm
        # or a config save right after a prayer would play it again.
        self._fired: set[tuple[str, str]] = set()

    async def async_start(self) -> None:
        """Arm today's prayers and a self-healing hourly recompute.

        The hourly tick (at :01) both rolls the day over at 00:01 AND re-arms
        every hour — so timers lost to a slept host, a dropped timer or a toggle
        that raced setup are back within the hour for the prayers still ahead,
        instead of a whole day going silent. Re-running reschedule() costs one
        offline prayer-time calc and a cancel/re-arm.
        """
        self._unsub_recompute = async_track_time_change(
            self._hass, self._handle_recompute, minute=1, second=0
        )
        self.reschedule()

    async def async_stop(self) -> None:
        """Cancel every armed timer (idempotent)."""
        self._cancel_prayers()
        if self._unsub_recompute is not None:
            self._unsub_recompute()
            self._unsub_recompute = None

    @callback
    def _handle_recompute(self, _now: datetime) -> None:
        self.reschedule()

    def schedule_snapshot(self) -> dict[str, Any]:
        """The last computed schedule (today's times + which are still ahead +
        target speakers), for the athan API's observability block."""
        return dict(self._schedule)

    def _cancel_prayers(self) -> None:
        """Cancel every armed prayer timer."""
        for unsub in self._unsub_prayers:
            unsub()
        self._unsub_prayers = []

    @callback
    def reschedule(self) -> None:
        """(Re)compute today's times and arm timers for the prayers still ahead.

        Safe to call any time — from setup, the hourly tick, or a config PUT.
        "Ahead" means not yet fired today and at most ``_GRACE_SEC`` past, so a
        hub that restarts just after a prayer still calls it, once. A no-op
        (all timers cleared) when athan is disabled or no location or timezone
        is resolvable.
        """
        self._cancel_prayers()
        resolved = self._resolve_config()
        if resolved is None:
            _LOGGER.debug("Athan: disabled or no location — nothing scheduled")
            self._schedule = {"enabled": False}
            return
        lat, lon, tz_name, method, school = resolved

        try:
            tz = dt_util.get_time_zone(tz_name)
        except ValueError:
            # zoneinfo raises (rather than "not found") for a malformed key
            # such as "../x". This runs during setup, so it must not raise.
            tz = None
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
                continue  # already fired, or well past (grace covers a late wake)
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

        # A configured selection that resolves to zero enrolled speakers means
        # athan is ENABLED but would fire NOWHERE — surface it loudly (visible
        # even when the hub logs at WARNING); it's a real misconfiguration.
        if has_sel and not targets:
            _LOGGER.warning(
                "Athan enabled for %s but its selected speakers are all un-enrolled — "
                "athan will fire on NO speakers until the selection is fixed.",
                today,
            )

        # The home's coordinates stay out of INFO: logs get pasted into public
        # issues. They are in the DEBUG line for troubleshooting.
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
        """``(has_selection, target_mac6s)``.

        No selection (absent/empty ``speakers``) => broadcast to ALL speakers.
        A selection is normalised and intersected with the currently-enrolled
        speakers, so a removed/renamed speaker silently drops out — an explicit
        selection NEVER falls back to blasting everyone (unlike PA).
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
        """The timer callback for one prayer on ``day`` (records it as fired)."""

        @callback
        def _fire(_now: datetime) -> None:
            self._fired.add((day, prayer))
            self._fire_athan(prayer)

        return _fire

    def _fire_athan(self, prayer: str) -> None:
        """Publish the athan play for ``prayer`` to its target speakers.

        A speaker the bus can't reach right now is logged and skipped: the
        adapter refuses rather than queues, so an athan never plays late.
        """
        # Re-check at fire time: the config may have been disabled since arming.
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

        # No selection => a single broadcast (mac=None). A selection => one
        # targeted play per chosen speaker (the same path PA uses).
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
        """Return ``(lat, lon, tz_name, method, school)`` or None if athan is off.

        Location and timezone fall back to the hub's own HA config so a hub
        configured with the home's location works with no app-side setup.
        Each coordinate falls back on its own.
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
