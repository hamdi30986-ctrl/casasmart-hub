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

from .audio import normalize_mac6, AudioError

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
        self._unsub_recompute: Optional[Any] = None
        # Last computed schedule, for the GET /audio/athan `schedule` block so
        # the app can show "next athan" and a silent failure can't hide.
        self._schedule: dict[str, Any] = {"enabled": False}

    async def async_start(self) -> None:
        """Arm today's prayers and a self-healing hourly recompute.

        The hourly tick (at :01) both rolls the day over at 00:01 AND re-arms
        every hour — so a missed trigger (a slept host, a dropped timer, a
        toggle that raced setup) self-corrects within the hour instead of
        silently missing a whole day. reschedule() is idempotent, so re-running
        it costs one offline prayer-time calc and cancel/re-arm.
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
            self._schedule = {"enabled": False}
            return
        lat, lon, tz_name, method, school = resolved

        tz = dt_util.get_time_zone(tz_name)
        if tz is None:
            _LOGGER.warning("Athan: unknown timezone %r — nothing scheduled", tz_name)
            self._schedule = {"enabled": True, "error": f"unknown timezone {tz_name!r}"}
            return

        today = datetime.now(tz).date()
        times = compute_prayer_times_utc(lat, lon, method, school, today.isoformat())
        if not times:
            _LOGGER.warning("Athan: could not compute prayer times for %s", today)
            self._schedule = {"enabled": True, "error": "prayer-time computation failed"}
            return

        athan = self._engine.get_athan() or {}
        has_sel, targets = self._resolve_targets(athan)

        now_utc = dt_util.utcnow()
        armed: list[str] = []
        prayers_out: list[dict[str, Any]] = []
        next_prayer: Optional[dict[str, Any]] = None
        for prayer in PRAYER_NAMES:
            fire_at = times.get(prayer)
            if fire_at is None:
                continue
            local = fire_at.astimezone(tz).strftime("%H:%M")
            upcoming = (fire_at - now_utc).total_seconds() >= -_GRACE_SEC
            prayers_out.append(
                {"name": prayer, "at": fire_at.isoformat(), "local": local, "upcoming": upcoming}
            )
            if not upcoming:
                continue  # already well past — skip (grace guards a late wake)
            unsub = async_track_point_in_time(
                self._hass, self._make_fire(prayer), fire_at
            )
            self._unsub_prayers.append(unsub)
            armed.append(f"{prayer} {local}")
            if next_prayer is None:
                next_prayer = {"name": prayer, "at": fire_at.isoformat(), "local": local}

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
        # even at the hub's default:warning level); it's a real misconfiguration.
        if has_sel and not targets:
            _LOGGER.warning(
                "Athan enabled for %s but its selected speakers are all un-enrolled — "
                "athan will fire on NO speakers until the selection is fixed.", today,
            )

        _LOGGER.info(
            "Athan scheduled for %s (%s/%s, %.4f,%.4f %s) on %s: %s",
            today, method, school, lat, lon, tz_name,
            "all speakers" if not has_sel
            else (f"{len(targets)} speaker(s): {','.join(targets)}" if targets
                  else "NO speakers (empty selection)"),
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
        except Exception:  # noqa: BLE001 — a registry hiccup must not kill firing
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
        athan = self._engine.get_athan() or {}
        has_sel, targets = self._resolve_targets(athan)
        file_path = f"{ATHAN_DIR}/{prayer.lower()}.mp3"

        if has_sel and not targets:
            _LOGGER.warning(
                "Athan: %s — selected speakers are all un-enrolled; firing nowhere", prayer
            )
            return

        # No selection => a single broadcast (mac=None). A selection => one
        # targeted play per chosen speaker (the same path PA uses).
        macs: list[Optional[str]] = targets if has_sel else [None]
        delivered = 0
        for mac in macs:
            try:
                topic, payload = self._engine.build_play(
                    mac=mac, file=file_path, priority="athan"
                )
            except Exception:  # noqa: BLE001 — a build error must not kill the loop
                _LOGGER.exception("Athan: failed to build %s play command", prayer)
                continue
            try:
                self._adapter.publish(topic, payload, qos=1)
                delivered += 1
            except Exception:  # noqa: BLE001 — a dead bus is non-fatal, just logged
                _LOGGER.warning(
                    "Athan: %s not delivered to %s — MQTT bus unavailable", prayer, topic
                )
        if delivered:
            _LOGGER.info(
                "Athan fired: %s -> %s", prayer,
                "all speakers" if not has_sel else f"{delivered} speaker(s): {','.join(targets)}",
            )

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
