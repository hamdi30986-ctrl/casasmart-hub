"""Builds and serves the room habit model on the hub (room_intel.habits).

HabitLearner reads the room activity log once a local day (one grouped
query per run of dates, storage.RoomActivityTable.habit_rows), builds every
layer of today's profile (Ramadan or regular, room_intel.habits), and keeps
them in memory and in kv now_room_habits: one doc per layer, so a restart the
same day reuses them instead of reading the log again. The model is rebuilt
when the date changes, when an admin changes what it learns from, when the
devices that count change, and after a member's data is deleted. Each Now
page request gets a HabitQuery for its layer; the all-off ranking asks it
which of the caller's rooms are usually on around now.

Each finished Ramadan is kept for the next one in kv now_ramadan_habits
(key: its Hijri year): the log's rows of its dates, grouped per room, device,
source, member and date as habit_rows reads them, minus the dates that may
not be learned. Next Ramadan's window reaches back to it, which the log's
400 days just cover and its row cap may not. The copy is written the day
after Ramadan ends (async_maintain, daily, whether learning is on or not;
every build checks too, so a restart or a hub that was off catches up),
read only while it is Ramadan, and deleted once no window reaches it (when
the next Ramadan ends), so it never holds anyone's activity longer than the
log's 400 days. It goes with a member's last device (their rows), a reset
and a factory reset; turning learning off keeps it, as it keeps the log.

The admin settings (learning on or off, pause, weekend days, devices on their
own schedule, reset) are kept in kv now_habit_config.

Nothing here imports Home Assistant: hass is used only for its executor and
its time zone.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..storage import StorageError
from .activity import HUMAN_RETENTION_DAYS
from .habits import (
    HUMAN_SOURCES,
    LAYER_HOUSEHOLD,
    LAYER_PERSONAL,
    MODEL_VERSION,
    PROFILE_RAMADAN,
    STATUS_LEARNING,
    STATUS_OFF,
    STATUS_PAUSED,
    STATUS_UNAVAILABLE,
    STATUS_WARMING_UP,
    WARM_UP_DAYS,
    HabitModel,
    HabitScorer,
    HabitSettings,
    UsableDays,
    build_model,
    layer_from_doc,
    layer_to_doc,
    local_day,
    profile_of,
    profile_window,
    signature,
)
from .hijri import RamadanMonth, next_ramadan, ramadan_of, ramadans_before

if TYPE_CHECKING:
    from ..storage import KeyValueTable, RoomActivityTable

_LOGGER = logging.getLogger(__name__)

_SETTINGS_KEY = "global"
_RAMADAN_VERSION = 1
# A kept Ramadan's row: room_id, entity_id, source, member_id, local_day,
# on_hours (habit_rows' fields, in that order).
_ROW_FIELDS = ("room_id", "entity_id", "source", "member_id", "local_day", "on_hours")


def _household_key(profile: str) -> str:
    return f"{profile}:household"


def _member_prefix(profile: str) -> str:
    return f"{profile}:member:"


# (reason, params, sort key) for a room in the all-off ranking's learned tier.
LearnedTier = Callable[[Mapping[str, Any]], tuple[str, dict, tuple] | None]


class HabitQuery:
    """One Now page request's view of the habits.

    member_id None is the household layer (the shared tablet). After
    async_learned, summary says which layer answered and how far learning
    is: {"layer": ..., "status": "off" | "paused" | "warming_up" |
    "learning" | "unavailable"}.
    """

    def __init__(
        self, learner: HabitLearner, member_id: str | None, profile: str
    ) -> None:
        self._learner = learner
        self.member_id = member_id
        self.summary: dict[str, str] = {
            "layer": LAYER_HOUSEHOLD if member_id is None else LAYER_PERSONAL,
            "status": STATUS_UNAVAILABLE,
            "profile": profile,
        }

    async def async_learned(
        self,
        room_ids: Iterable[str],
        counted: Collection[str],
        now_t: float | None = None,
    ) -> LearnedTier | None:
        """The learned tier for the caller's rooms, or None (no habits now).

        counted are the devices that make a room on (ranking's rule), wherever
        they are.
        """
        return await self._learner.async_learned(self, room_ids, counted, now_t)


class HabitLearner:
    """The room habit model and its settings, for the whole hub."""

    def __init__(
        self,
        hass: Any,
        model_table: KeyValueTable,
        config_table: KeyValueTable,
        activity_table: RoomActivityTable,
        *,
        ramadan_table: KeyValueTable,
        recorder: Any = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._hass = hass
        self._models = model_table
        self._config = config_table
        self._activity = activity_table
        self._ramadans = ramadan_table
        self._recorder = recorder
        self._wall_clock = wall_clock
        self._settings = HabitSettings()
        self._lock = asyncio.Lock()
        self._model: HabitModel | None = None
        self._model_key: tuple | None = None
        self._read_failed = False
        # How many times the model was built from the log, and loaded from
        # its saved docs (for tests and the report's cost figures).
        self.builds = 0
        self.loads = 0

    async def async_start(self) -> None:
        """Read the settings (the model is built when first asked for)."""
        try:
            doc = await self._hass.async_add_executor_job(
                self._config.get, _SETTINGS_KEY
            )
        except (StorageError, sqlite3.Error) as err:
            _LOGGER.warning("Room habits: settings could not be read: %s", err)
            return
        self._settings = HabitSettings.from_doc(doc)

    @property
    def settings(self) -> HabitSettings:
        return self._settings

    def query(self, member_id: str | None) -> HabitQuery:
        """A Now page request's view: the member's blend, or None for the household."""
        return HabitQuery(self, member_id, self.profile_at(self._wall_clock()))

    def profile_at(self, now_t: float) -> str:
        """The profile the habits answer from at now_t (ramadan or regular)."""
        return profile_of(self._local_now(now_t)[0])

    # -- the ranking's question ------------------------------------------------

    async def async_learned(
        self,
        query: HabitQuery,
        room_ids: Iterable[str],
        counted: Collection[str],
        now_t: float | None,
    ) -> LearnedTier | None:
        settings = self._settings
        now_t = self._wall_clock() if now_t is None else now_t
        query.summary["profile"] = self.profile_at(now_t)
        if not settings.learning:
            query.summary["status"] = STATUS_OFF
            return None
        model = await self._async_model(counted, now_t)
        if model is None:
            query.summary["status"] = STATUS_UNAVAILABLE
            return None
        scorer = self._scorer(model, room_ids, now_t, query.member_id)
        if settings.paused:
            query.summary["status"] = STATUS_PAUSED
        elif not scorer.ready:
            query.summary["status"] = STATUS_WARMING_UP
        else:
            query.summary["status"] = STATUS_LEARNING
        return scorer.learned if scorer.ready else None

    # -- admin -----------------------------------------------------------------

    async def async_settings(
        self,
        room_ids: Iterable[str],
        counted: Collection[str],
        now_t: float | None = None,
    ) -> dict[str, Any]:
        """The settings and how far learning is, for the admin."""
        settings = self._settings
        now_t = self._wall_clock() if now_t is None else now_t
        observed: int | None = None
        status = settings.status
        if settings.learning:
            model = await self._async_model(counted, now_t)
            if model is None:
                status = STATUS_UNAVAILABLE
            else:
                scorer = self._scorer(model, room_ids, now_t, None)
                observed = scorer.observed_days
                if status == STATUS_LEARNING and not scorer.ready:
                    status = STATUS_WARMING_UP
        today = self._local_now(now_t)[0]
        month = next_ramadan(today)
        try:
            kept = await self._hass.async_add_executor_job(self._kept_years)
        except (StorageError, sqlite3.Error) as err:
            _LOGGER.debug("Room habits: kept Ramadans could not be listed: %s", err)
            kept = None
        return {
            "learning": settings.learning,
            "paused": settings.paused,
            "status": status,
            "weekend_days": list(settings.weekend_days),
            "self_scheduled_entity_ids": list(settings.self_scheduled_entity_ids),
            "reset_at": None
            if settings.reset_at is None
            else datetime.fromtimestamp(settings.reset_at, UTC).isoformat(),
            "observed_days": observed,
            "warm_up_days": WARM_UP_DAYS,
            "profile": profile_of(today),
            "ramadan": None
            if month is None
            else {
                "year": month.year,
                "first_day": _iso_day(month.first),
                "last_day": _iso_day(month.last),
                "kept_years": kept,
            },
        }

    async def async_update(
        self, payload: Any, *, managed_ids: Collection[str], now_t: float | None = None
    ) -> HabitSettings:
        """Apply an admin's change (HabitSettingsError when it is invalid)."""
        now = int(self._wall_clock() if now_t is None else now_t)
        async with self._lock:
            before = self._settings
            after = before.updated(payload, now, managed_ids=managed_ids)
            # Learning off keeps no model; nothing else needs a rebuild now.
            await self._hass.async_add_executor_job(
                self._save_settings, after, not after.learning
            )
            self._settings = after
            if not after.learning or after.model_key() != before.model_key():
                self._drop_model()
        return after

    async def async_maintain(self, _now: datetime | None = None) -> None:
        """Keep each finished Ramadan and delete the copies no window reaches.

        Daily, whether learning is on or not, so a Ramadan that ends while
        learning is off is still kept for when it comes back on.
        """
        today, _, offset = self._local_now(self._wall_clock())
        async with self._lock:
            try:
                if self._recorder is not None:
                    await self._recorder.async_flush()
                await self._hass.async_add_executor_job(
                    self._keep_finished_ramadans, today, offset
                )
            except (StorageError, sqlite3.Error) as err:
                _LOGGER.warning("Room habits: Ramadan could not be kept: %s", err)

    async def async_reset(self, now_t: float | None = None) -> HabitSettings:
        """Forget every habit learned so far, the kept Ramadans too; learning
        starts over tomorrow."""
        now = int(self._wall_clock() if now_t is None else now_t)
        async with self._lock:
            after = self._settings.reset(now)
            await self._hass.async_add_executor_job(
                self._save_settings, after, True, True
            )
            self._settings = after
            self._drop_model()
        return after

    async def async_forget_member(self, member_id: str) -> None:
        """Delete what was learned from a member (their layer, their share of
        the household's, their rows in the kept Ramadans). Their log rows
        must already be deleted: the next request rebuilds from what is
        left."""
        async with self._lock:

            def forget() -> None:
                member = _ROW_FIELDS.index("member_id")
                with self._models.transaction():
                    self._models.clear()
                    for key, doc in self._ramadans.items():
                        rows = doc.get("rows") if isinstance(doc, dict) else None
                        if not isinstance(rows, list):
                            # Unreadable: it can't be learned from; don't
                            # leave anything of theirs behind in it.
                            del self._ramadans[key]
                            continue
                        kept = [
                            row
                            for row in rows
                            if isinstance(row, list)
                            and len(row) == len(_ROW_FIELDS)
                            and row[member] != member_id
                        ]
                        if len(kept) != len(rows):
                            self._ramadans[key] = {**doc, "rows": kept}

            await self._hass.async_add_executor_job(forget)
            self._drop_model()

    async def async_clear(self) -> None:
        """Delete the model, the kept Ramadans and the settings (factory reset)."""
        async with self._lock:

            def clear() -> None:
                with self._models.transaction():
                    self._models.clear()
                    self._ramadans.clear()
                    self._config.clear()

            await self._hass.async_add_executor_job(clear)
            self._settings = HabitSettings()
            self._drop_model()

    # -- the model -------------------------------------------------------------

    def _scorer(
        self,
        model: HabitModel,
        room_ids: Iterable[str],
        now_t: float,
        member_id: str | None,
    ) -> HabitScorer:
        today, hour, _ = self._local_now(now_t)
        return HabitScorer(
            model,
            room_ids,
            today=today,
            hour=hour,
            weekend_days=self._settings.weekend_days,
            member_id=member_id,
        )

    async def _async_model(
        self, counted: Collection[str], now_t: float
    ) -> HabitModel | None:
        """Today's model, built (or loaded) once and then served from memory."""
        counted = frozenset(counted)
        today, _, offset = self._local_now(now_t)

        def key() -> tuple:
            return (today, offset, counted, self._settings.model_key())

        if self._model is not None and self._model_key == key():
            return self._model
        async with self._lock:
            if self._model is not None and self._model_key == key():
                return self._model
            settings = self._settings
            usable = settings.usable_days(today, offset)
            scheduled = frozenset(settings.self_scheduled_entity_ids)
            sig = signature(
                counted,
                scheduled,
                [settings.reset_at],
                settings.excluded,
                [offset],
            )
            if self._recorder is not None:
                # Yesterday's last few seconds may still be in its buffer.
                await self._recorder.async_flush()
            try:
                model, built = await self._hass.async_add_executor_job(
                    self._load_or_build, today, offset, sig, usable, counted, scheduled
                )
            except (StorageError, sqlite3.Error, ValueError) as err:
                log = _LOGGER.debug if self._read_failed else _LOGGER.warning
                log("Room habits could not be built: %s", err)
                self._read_failed = True
                return None
            self._read_failed = False
            if built:
                self.builds += 1
            else:
                self.loads += 1
            self._model, self._model_key = model, key()
            return model

    def _load_or_build(
        self,
        today: int,
        offset: int,
        sig: str,
        usable: UsableDays,
        counted: frozenset[str],
        scheduled: frozenset[str],
    ) -> tuple[HabitModel, bool]:
        """The saved model when it was built today from the same inputs, else
        a new one from the log (and the kept Ramadans), saved (blocking:
        executor). (model, built)."""
        self._keep_finished_ramadans(today, offset)
        profile = usable.profile
        header = {"v": MODEL_VERSION, "profile": profile, "day": today, "sig": sig}
        prefix = _member_prefix(profile)
        docs = dict(self._models.items())
        household = docs.pop(_household_key(profile), None)
        if _matches(household, header) and all(
            key.startswith(prefix) and _matches(doc, header)
            for key, doc in docs.items()
        ):
            try:
                return (
                    HabitModel(
                        layer_from_doc(household["layer"]),
                        {
                            key[len(prefix) :]: layer_from_doc(doc["layer"])
                            for key, doc in docs.items()
                        },
                        usable.ages,
                    ),
                    False,
                )
            except (KeyError, TypeError, ValueError) as err:
                # A doc with the right header but a bad or missing layer.
                _LOGGER.debug(
                    "Room habits: saved model unreadable, rebuilding: %s", err
                )
        rows: list[dict[str, Any]] = []
        for first, last in usable.spans():
            month = ramadan_of(first)
            if profile == PROFILE_RAMADAN and month is not None and month.last < today:
                # A finished Ramadan: from its kept copy, not the log.
                rows.extend(
                    row
                    for row in self._kept_rows(month)
                    if first <= row["local_day"] <= last
                )
                continue
            rows.extend(
                self._activity.habit_rows(
                    first_day=max(0, first),
                    last_day=max(0, last),
                    sources=HUMAN_SOURCES,
                )
            )
        model = build_model(
            rows, counted=counted, self_scheduled=scheduled, usable=usable
        )
        model.ages = usable.ages
        with self._models.transaction():
            self._models.clear()
            self._models[_household_key(profile)] = {
                **header,
                "layer": layer_to_doc(model.household),
            }
            for member_id, layer in model.members.items():
                self._models[prefix + member_id] = {
                    **header,
                    "layer": layer_to_doc(layer),
                }
        return model, True

    # -- the kept Ramadans -----------------------------------------------------

    def _keep_finished_ramadans(self, today: int, offset: int) -> None:
        """Copy each finished Ramadan the log still holds and no copy has
        yet; delete the copies no Ramadan window can reach (blocking)."""
        stored = set(self._ramadans)
        upcoming = next_ramadan(today)
        # The Ramadans the next window (or this one) reaches, None past the
        # table: keep everything.
        needed = None
        if upcoming is not None:
            needed = {upcoming.year} | {
                month.year
                for day in profile_window(upcoming.first, PROFILE_RAMADAN)
                if (month := ramadan_of(day)) is not None
            }
        copies: dict[str, dict[str, Any]] = {}
        for month in ramadans_before(today):
            if needed is not None and month.year not in needed:
                break
            key = str(month.year)
            if (
                key in stored
                or month.last >= today
                or month.last < today - HUMAN_RETENTION_DAYS
            ):
                continue
            blocked = self._settings.blocked_days(
                range(month.first, month.last + 1), offset
            )
            rows = self._activity.habit_rows(
                first_day=month.first, last_day=month.last, sources=HUMAN_SOURCES
            )
            copies[key] = {
                "v": _RAMADAN_VERSION,
                "year": month.year,
                "first_day": month.first,
                "last_day": month.last,
                "rows": [
                    [row[name] for name in _ROW_FIELDS]
                    for row in rows
                    if row["local_day"] not in blocked
                ],
            }
        # Only older ones: a clock gone back (no RTC, before NTP) must not
        # delete what a later Ramadan needs.
        oldest = None if needed is None else min(needed)
        gone = [
            key
            for key in stored
            if not key.isdigit() or (oldest is not None and int(key) < oldest)
        ]
        if not copies and not gone:
            return
        with self._ramadans.transaction():
            for key, doc in copies.items():
                self._ramadans[key] = doc
            for key in gone:
                del self._ramadans[key]

    def _kept_rows(self, month: RamadanMonth) -> list[dict[str, Any]]:
        """A finished Ramadan's kept rows (none when it has no readable copy)."""
        try:
            doc = self._ramadans[str(month.year)]
        except KeyError:
            return []
        rows = _kept(doc, month)
        if rows is None:
            _LOGGER.debug("Room habits: kept Ramadan %s unreadable", month.year)
            return []
        return rows

    def _kept_years(self) -> list[int]:
        return sorted(int(key) for key in self._ramadans if key.isdigit())

    def _save_settings(
        self, settings: HabitSettings, clear_model: bool, clear_ramadans: bool = False
    ) -> None:
        with self._config.transaction():
            self._config[_SETTINGS_KEY] = settings.to_doc()
            if clear_model:
                self._models.clear()
            if clear_ramadans:
                self._ramadans.clear()

    def _drop_model(self) -> None:
        self._model = None
        self._model_key = None

    def _local_now(self, now_t: float) -> tuple[int, int, int]:
        """(local day, local hour, UTC offset in minutes) at now_t."""
        try:
            zone = ZoneInfo(self._hass.config.time_zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            zone = UTC
        local = datetime.fromtimestamp(now_t, zone)
        offset = local.utcoffset()
        minutes = int(offset.total_seconds() // 60) if offset is not None else 0
        return local_day(now_t, minutes), local.hour, minutes


def _matches(doc: Any, header: Mapping[str, Any]) -> bool:
    return isinstance(doc, dict) and all(doc.get(k) == v for k, v in header.items())


def _kept(doc: Any, month: RamadanMonth) -> list[dict[str, Any]] | None:
    """A kept Ramadan's rows as habit_rows gives them, or None when the doc
    isn't a copy of that Hijri year.

    Rows outside the month's dates are dropped, so a later correction of a
    year in the Umm al-Qura table keeps the copy (minus a day at most).
    """
    if (
        not isinstance(doc, dict)
        or doc.get("v") != _RAMADAN_VERSION
        or doc.get("year") != month.year
    ):
        return None
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return None
    kept = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(_ROW_FIELDS):
            return None
        room_id, entity_id, source, member_id, day, on_hours = row
        if not (
            isinstance(room_id, str)
            and isinstance(entity_id, str)
            and isinstance(source, str)
            and (member_id is None or isinstance(member_id, str))
            and _is_day(day)
            and _is_day(on_hours)
        ):
            return None
        if month.first <= day <= month.last:
            kept.append(dict(zip(_ROW_FIELDS, row, strict=True)))
    return kept


def _is_day(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _iso_day(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).date().isoformat()
