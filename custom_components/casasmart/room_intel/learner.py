"""Builds and serves the room habit model on the hub (room_intel.habits).

HabitLearner reads the room activity log once a local day (one grouped
query, storage.RoomActivityTable.habit_rows), builds every layer, and keeps
them in memory and in kv now_room_habits: one doc per layer, so a restart the
same day reuses them instead of reading the log again. The model is rebuilt
when the date changes, when an admin changes what it learns from, when the
devices that count change, and after a member's data is deleted. Each Now
page request gets a HabitQuery for its layer; the all-off ranking asks it
which of the caller's rooms are usually on around now.

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
from .habits import (
    HUMAN_SOURCES,
    LAYER_HOUSEHOLD,
    LAYER_PERSONAL,
    MODEL_VERSION,
    PROFILE_REGULAR,
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
    signature,
)

if TYPE_CHECKING:
    from ..storage import KeyValueTable, RoomActivityTable

_LOGGER = logging.getLogger(__name__)

_SETTINGS_KEY = "global"
_HOUSEHOLD_KEY = f"{PROFILE_REGULAR}:household"
_MEMBER_PREFIX = f"{PROFILE_REGULAR}:member:"

# (reason, params, sort key) for a room in the all-off ranking's learned tier.
LearnedTier = Callable[[Mapping[str, Any]], tuple[str, dict, tuple] | None]


class HabitQuery:
    """One Now page request's view of the habits.

    member_id None is the household layer (the shared tablet). After
    async_learned, summary says which layer answered and how far learning
    is: {"layer": ..., "status": "off" | "paused" | "warming_up" |
    "learning" | "unavailable"}.
    """

    def __init__(self, learner: HabitLearner, member_id: str | None) -> None:
        self._learner = learner
        self.member_id = member_id
        self.summary: dict[str, str] = {
            "layer": LAYER_HOUSEHOLD if member_id is None else LAYER_PERSONAL,
            "status": STATUS_UNAVAILABLE,
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
        recorder: Any = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._hass = hass
        self._models = model_table
        self._config = config_table
        self._activity = activity_table
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
        return HabitQuery(self, member_id)

    # -- the ranking's question ------------------------------------------------

    async def async_learned(
        self,
        query: HabitQuery,
        room_ids: Iterable[str],
        counted: Collection[str],
        now_t: float | None,
    ) -> LearnedTier | None:
        settings = self._settings
        if not settings.learning:
            query.summary["status"] = STATUS_OFF
            return None
        now_t = self._wall_clock() if now_t is None else now_t
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

    async def async_reset(self, now_t: float | None = None) -> HabitSettings:
        """Forget every habit learned so far; learning starts over tomorrow."""
        now = int(self._wall_clock() if now_t is None else now_t)
        async with self._lock:
            after = self._settings.reset(now)
            await self._hass.async_add_executor_job(self._save_settings, after, True)
            self._settings = after
            self._drop_model()
        return after

    async def async_forget_member(self, member_id: str) -> None:
        """Delete what was learned from a member (their layer and their share
        of the household's). Their log rows must already be deleted: the next
        request rebuilds from what is left."""
        async with self._lock:
            await self._hass.async_add_executor_job(self._models.clear)
            self._drop_model()

    async def async_clear(self) -> None:
        """Delete the model and the settings (factory reset)."""
        async with self._lock:

            def clear() -> None:
                with self._models.transaction():
                    self._models.clear()
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
                    self._load_or_build, today, sig, usable, counted, scheduled
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
        sig: str,
        usable: UsableDays,
        counted: frozenset[str],
        scheduled: frozenset[str],
    ) -> tuple[HabitModel, bool]:
        """The saved model when it was built today from the same inputs, else
        a new one from the log, saved (blocking: executor). (model, built)."""
        header = {"v": MODEL_VERSION, "day": today, "sig": sig}
        docs = dict(self._models.items())
        household = docs.pop(_HOUSEHOLD_KEY, None)
        if _matches(household, header) and all(
            key.startswith(_MEMBER_PREFIX) and _matches(doc, header)
            for key, doc in docs.items()
        ):
            try:
                return (
                    HabitModel(
                        layer_from_doc(household["layer"]),
                        {
                            key[len(_MEMBER_PREFIX) :]: layer_from_doc(doc["layer"])
                            for key, doc in docs.items()
                        },
                    ),
                    False,
                )
            except ValueError as err:
                _LOGGER.debug(
                    "Room habits: saved model unreadable, rebuilding: %s", err
                )
        rows = self._activity.habit_rows(
            first_day=max(0, usable.first),
            last_day=max(0, usable.last),
            sources=HUMAN_SOURCES,
        )
        model = build_model(
            rows, counted=counted, self_scheduled=scheduled, usable=usable
        )
        with self._models.transaction():
            self._models.clear()
            self._models[_HOUSEHOLD_KEY] = {
                **header,
                "layer": layer_to_doc(model.household),
            }
            for member_id, layer in model.members.items():
                self._models[_MEMBER_PREFIX + member_id] = {
                    **header,
                    "layer": layer_to_doc(layer),
                }
        return model, True

    def _save_settings(self, settings: HabitSettings, clear_model: bool) -> None:
        with self._config.transaction():
            self._config[_SETTINGS_KEY] = settings.to_doc()
            if clear_model:
                self._models.clear()

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
