"""Hub-native athan (prayer-call) scheduler.

Computes the five daily prayer times locally via ``prayer-times-calculator-offline``
(the same offline, no-network library Home Assistant's *Islamic Prayer Times*
integration uses) from the athan config the app stores through ``PUT /audio/athan``
(``lat``/``lon``/``timezone``/``method``/``school``), falling back to the hub's own
configured location (``hass.config.latitude/longitude/time_zone``). It arms one HA
timer per prayer and, at prayer time, publishes a broadcast ``play`` command
(priority ``athan``) through the audio adapter — the same play path PA uses.

This replaces the standalone ``casaos-athan-scheduler`` daemon. The hub owns
audio (B14), so it owns athan scheduling too. The library returns UTC timestamps,
so DST/offset handling is inherent (no fixed table), and it adds Hanafi/Shafi Asr,
high-latitude rules and ~24 regional calculation methods — correct in any region,
with no Supabase, no hardcoded home id and no separate broker credentials.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo  # noqa: F401  (tzinfo used in hints)
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

# The library's accepted calculation methods (lower-case). The app historically
# sends "egyptian"; the library calls it "egypt", so alias it.
_LIB_METHODS = frozenset({
    "mwl", "isna", "egypt", "makkah", "karachi", "tehran", "jafari", "gulf",
    "kuwait", "qatar", "singapore", "france", "turkey", "russia", "moonsighting",
    "dubai", "jakim", "tunisia", "algeria", "kemenag", "morocco", "portugal",
    "jordan", "custom",
})
_METHOD_ALIASES = {"egyptian": "egypt", "umalqura": "makkah", "umm_al_qura": "makkah"}
_DEFAULT_METHOD = "makkah"
_ASR_SCHOOLS = frozenset({"shafi", "hanafi"})


def compute_prayer_times_utc(
    lat: float, lon: float, method: str, school: str, date_str: str
) -> Optional[dict[str, datetime]]:
    """The five prayer times as timezone-aware UTC datetimes for ``date_str``.

    Uses ``prayer-times-calculator-offline`` (pure local math, no network).
    Returns None if the library is missing or the calculation fails, so the
    caller schedules nothing rather than crashing the loop.
    """
    try:
        from prayer_times_calculator_offline import PrayerTimesCalculator
    except Exception:  # noqa: BLE001 — declared in manifest; guard anyway
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
    except Exception:  # noqa: BLE001 — a bad config must not kill the loop
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
            dt = dt.replace(tzinfo=timezone.utc)
        out[prayer] = dt
    return out or None


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
        lat, lon, tz_name, method, school = resolved

        tz = dt_util.get_time_zone(tz_name)
        if tz is None:
            _LOGGER.warning("Athan: unknown timezone %r — nothing scheduled", tz_name)
            return

        today = datetime.now(tz).date()
        times = compute_prayer_times_utc(lat, lon, method, school, today.isoformat())
        if not times:
            _LOGGER.warning("Athan: could not compute prayer times for %s", today)
            return

        now_utc = dt_util.utcnow()
        armed: list[str] = []
        for prayer in PRAYER_NAMES:
            fire_at = times.get(prayer)
            if fire_at is None:
                continue
            if (fire_at - now_utc).total_seconds() < -_GRACE_SEC:
                continue  # already well past — skip (grace guards a late wake)
            unsub = async_track_point_in_time(
                self._hass, self._make_fire(prayer), fire_at
            )
            self._unsub_prayers.append(unsub)
            armed.append(f"{prayer} {fire_at.astimezone(tz).strftime('%H:%M')}")

        _LOGGER.info(
            "Athan scheduled for %s (%s/%s, %.4f,%.4f %s): %s",
            today, method, school, lat, lon, tz_name,
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

    def _resolve_config(
        self,
    ) -> Optional[tuple[float, float, str, str, str]]:
        """Return ``(lat, lon, tz_name, method, school)`` or None if athan is off.

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
        method = athan.get("method") or _DEFAULT_METHOD
        school = athan.get("school") or "shafi"
        try:
            return float(lat), float(lon), str(tz_name), str(method), str(school)
        except (TypeError, ValueError):
            return None
