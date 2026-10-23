"""Room habits: when each room is usually switched on, learned from the log.

The room activity log (room_intel.activity, storage.RoomActivityTable) is the
only source, never Home Assistant's recorder. room_intel.learner builds the
model on the hub once a local day and keeps it in kv now_room_habits; the
all-off ranking asks it which rooms are usually on around now. Nothing here
imports Home Assistant.

Votes. A confirmed "on" of a device that makes its room on (the ranking's
rule: a light, a fan, a switch whose gang is a light, fan, switch or outlet,
or an AC) votes for its room, local date and local hour, once however often
it was tapped. A member's app tap, a scene a member ran, a Home Assistant
user and a wall switch (a press, a remote) weigh 1; a wall switch has no
person behind it, so it votes only in the household layer, never in a
member's (wall presses at full weight, was 1/2).
Automations and unnamed service callers weigh 0. A device an admin marked
"runs on its own schedule" has its wall switch events ignored: a schedule
kept on the device logs as a wall switch.

Layers. The household layer learns from every vote. A member's own layer
learns from their app taps only. The shared tablet asks for the household; a
phone gets its member's layer blended over the household's, the member's
counting more as their own history grows:
lambda = min(0.8, N / (N + 10)), N = dates they switched something on from
the app in their rooms.

Strength of a room at local hour h, over the observed dates d of today's day
class (workday or weekend) in the last WINDOW_DAYS, today excluded:

    x_d   = max(v(d, h), 1/2 v(d, h-1), 1/2 v(d, h+1))   v = the vote's weight
    w_d   = 2 ** -(age_d / HALF_LIFE_DAYS)
    habit = sum(w_d x_d) / (sum(w_d) + PRIOR_DAYS)

The neighbouring hours run across midnight. A date is observed when someone
did something by hand in one of the asker's rooms (for a member's layer:
their own app taps), so days away and days the hub was down don't count as
"not used", and a member's answer never depends on rooms they can't see. The
PRIOR_DAYS empty days keep one evening from looking like a habit: 0.35 is
about "on at this hour on more than half of the recent days". A room is
usually on around now with habit >= MIN_STRENGTH on at least
MIN_EVIDENCE_DAYS dates, and only once the asker's rooms have WARM_UP_DAYS
observed dates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .activity import (
    SOURCE_APP,
    SOURCE_HA_USER,
    SOURCE_SCENE,
    SOURCE_WALL_SWITCH,
)
from .off_rooms import LEARNED_HABIT, REASON_HABIT_NOW

HALF_LIFE_DAYS = 14
WINDOW_DAYS = 56
PRIOR_DAYS = 4
MIN_STRENGTH = 0.35
MIN_EVIDENCE_DAYS = 3
# "Kicks in after 2-4 weeks" (the approved plan): nothing ranks on a habit
# until the asker's rooms have this many observed dates.
WARM_UP_DAYS = 14
NEIGHBOUR_WEIGHT = 0.5
# A member's own layer weighs min(0.8, N / (N + 10)) against the household's
# (N = dates they switched something on from the app).
PERSONAL_WEIGHT_MAX = 0.8
PERSONAL_WEIGHT_HALF = 10

# The sources that vote, and their weight in the household layer. Any other
# source (automation, unknown) weighs 0. A weight under 1 would be kept as a
# half vote (RoomEvidence.half); none is today: wall switches weighed 1/2
# until they were set to 1, in the household layer only.
SOURCE_WEIGHTS: Mapping[str, float] = {
    SOURCE_APP: 1.0,
    SOURCE_SCENE: 1.0,
    SOURCE_HA_USER: 1.0,
    SOURCE_WALL_SWITCH: 1.0,
}
HUMAN_SOURCES = tuple(SOURCE_WEIGHTS)
# The source of a member's own layer: their app taps (never a wall switch).
PERSONAL_SOURCE = SOURCE_APP

# Saudi weekend (Friday, Saturday); Monday = 0.
DEFAULT_WEEKEND_DAYS = (4, 5)
MAX_SELF_SCHEDULED = 256
# Pause and off periods the settings keep (older ones fall out of the window).
_MAX_EXCLUDED = 64

LAYER_HOUSEHOLD = "household"
LAYER_PERSONAL = "personal"
LAYERS = (LAYER_HOUSEHOLD, LAYER_PERSONAL)

STATUS_OFF = "off"
STATUS_PAUSED = "paused"
STATUS_WARMING_UP = "warming_up"
STATUS_LEARNING = "learning"
STATUS_UNAVAILABLE = "unavailable"

# One profile today. Ramadan can be learned as a profile of its own (its dates
# leave the regular profile's usable days); the model docs carry it in their
# key so the two never mix.
PROFILE_REGULAR = "regular"
# 2: wall switches vote at full weight (a saved v1 model has them at half).
MODEL_VERSION = 2

_SETTINGS_FIELDS = frozenset(
    {"learning", "paused", "weekend_days", "self_scheduled_entity_ids"}
)


class HabitSettingsError(ValueError):
    """A habit setting is invalid."""


def local_day(t: float, utc_offset_min: int) -> int:
    """The local date of epoch time t as a day number (1970-01-01 = 0)."""
    return int((t + utc_offset_min * 60) // 86400)


def weekday(day: int) -> int:
    """The weekday of a day number, Monday = 0 (day 0 was a Thursday)."""
    return (day + 3) % 7


def personal_weight(dates: int) -> float:
    """How much a member's own layer counts after `dates` dates of taps."""
    return min(PERSONAL_WEIGHT_MAX, dates / (dates + PERSONAL_WEIGHT_HALF))


# -- the model ------------------------------------------------------------------


@dataclass
class RoomEvidence:
    """One room's votes in one layer.

    seen holds the observed dates; full and half each date's mask of hours
    (bit h) with a full or a half vote.
    """

    seen: set[int] = field(default_factory=set)
    full: dict[int, int] = field(default_factory=dict)
    half: dict[int, int] = field(default_factory=dict)

    def vote(self, day: int, hour: int, *, half_weight: bool) -> float:
        if self.full.get(day, 0) >> hour & 1:
            return 1.0
        if half_weight and self.half.get(day, 0) >> hour & 1:
            return 0.5
        return 0.0

    def near(self, day: int, hour: int, *, half_weight: bool) -> float:
        """x_d: the vote at the hour, or half a vote an hour either side."""
        before = (day, hour - 1) if hour else (day - 1, 23)
        after = (day, hour + 1) if hour < 23 else (day + 1, 0)
        return max(
            self.vote(day, hour, half_weight=half_weight),
            NEIGHBOUR_WEIGHT * self.vote(*before, half_weight=half_weight),
            NEIGHBOUR_WEIGHT * self.vote(*after, half_weight=half_weight),
        )

    def vote_dates(self) -> set[int]:
        """The dates the room got a full vote."""
        return {day for day, mask in self.full.items() if mask}


# room_id -> evidence
Layer = dict[str, RoomEvidence]


@dataclass
class HabitModel:
    """Every layer, for the dates of one window."""

    household: Layer = field(default_factory=dict)
    members: dict[str, Layer] = field(default_factory=dict)


def build_model(
    rows: Iterable[Mapping[str, Any]],
    *,
    counted: Collection[str],
    self_scheduled: Collection[str],
    usable: Callable[[int], bool],
) -> HabitModel:
    """Build the layers from RoomActivityTable.habit_rows rows.

    counted are the devices whose "on" makes a room on; self_scheduled the
    devices whose wall switch events don't count; usable says which local
    dates may be learned (the window, minus pauses and before a reset).
    """
    model = HabitModel()
    for row in rows:
        day = row["local_day"]
        source = row["source"]
        weight = SOURCE_WEIGHTS.get(source, 0.0)
        entity_id = row["entity_id"]
        if (
            not weight
            or not usable(day)
            or (source == SOURCE_WALL_SWITCH and entity_id in self_scheduled)
        ):
            continue
        room_id = row["room_id"]
        on_hours = row["on_hours"] if entity_id in counted else 0
        household = model.household.setdefault(room_id, RoomEvidence())
        household.seen.add(day)
        if on_hours:
            votes = household.full if weight >= 1.0 else household.half
            votes[day] = votes.get(day, 0) | on_hours
        member_id = row["member_id"]
        if source == PERSONAL_SOURCE and member_id:
            own = model.members.setdefault(member_id, {}).setdefault(
                room_id, RoomEvidence()
            )
            own.seen.add(day)
            if on_hours:
                own.full[day] = own.full.get(day, 0) | on_hours
    return model


def strength(
    evidence: RoomEvidence | None,
    observed: Iterable[int],
    *,
    hour: int,
    today: int,
    half_weight: bool = True,
) -> tuple[float, int]:
    """(habit, dates with evidence) for a room at an hour, over observed dates."""
    weighted = total = 0.0
    dates = 0
    for day in observed:
        weight = 2.0 ** (-(today - day) / HALF_LIFE_DAYS)
        total += weight
        if evidence is None:
            continue
        x = evidence.near(day, hour, half_weight=half_weight)
        if x:
            weighted += weight * x
            dates += 1
    return weighted / (total + PRIOR_DAYS), dates


class HabitScorer:
    """Which rooms are usually on now, for one asker at one moment.

    room_ids are the asker's rooms (their scope); nothing outside them is
    read. member_id None asks for the household layer; a member id for
    theirs blended over it (personal_weight).
    """

    def __init__(
        self,
        model: HabitModel,
        room_ids: Iterable[str],
        *,
        today: int,
        hour: int,
        weekend_days: Collection[int],
        member_id: str | None = None,
    ) -> None:
        rooms = list(dict.fromkeys(room_ids))
        self._model = model
        self._today = today
        self._hour = hour
        self._member = (
            model.members.get(member_id, {}) if member_id is not None else None
        )
        weekend = weekday(today) in weekend_days

        def observed(layer: Layer) -> tuple[int, list[int]]:
            days = set().union(*(layer[r].seen for r in rooms if r in layer))
            same_class = sorted(
                d for d in days if (weekday(d) in weekend_days) == weekend
            )
            return len(days), same_class

        self.observed_days, self._observed = observed(model.household)
        self._own_observed: list[int] = []
        # How much the member's own layer counts (0 for the household).
        self.personal_weight = 0.0
        if self._member is not None:
            self._own_observed = observed(self._member)[1]
            own = self._member
            self.personal_weight = personal_weight(
                len(set().union(*(own[r].vote_dates() for r in rooms if r in own)))
            )

    @property
    def ready(self) -> bool:
        """True once the asker's rooms have enough history to rank on."""
        return self.observed_days >= WARM_UP_DAYS

    def score(self, room_id: str) -> tuple[float, int]:
        """(strength, household dates with evidence) of a room at this hour."""
        habit, dates = strength(
            self._model.household.get(room_id),
            self._observed,
            hour=self._hour,
            today=self._today,
        )
        weight = self.personal_weight
        if weight:
            mine, _ = strength(
                self._member.get(room_id),
                self._own_observed,
                hour=self._hour,
                today=self._today,
                half_weight=False,
            )
            habit = weight * mine + (1 - weight) * habit
        return habit, dates

    def learned(self, room: Mapping[str, Any]) -> tuple[str, dict, tuple] | None:
        """The ranking's learned tier for a room: habit_now, or None."""
        if not self.ready:
            return None
        habit, dates = self.score(room["room_id"])
        if habit < MIN_STRENGTH or dates < MIN_EVIDENCE_DAYS:
            return None
        return REASON_HABIT_NOW, {}, (LEARNED_HABIT, -round(habit, 4))


# -- storage shape --------------------------------------------------------------


def layer_to_doc(layer: Layer) -> dict[str, Any]:
    """A layer as JSON: per room, observed dates and [date, full, half] votes."""
    return {
        room_id: {
            "seen": sorted(evidence.seen),
            "on": [
                [day, evidence.full.get(day, 0), evidence.half.get(day, 0)]
                for day in sorted(evidence.full.keys() | evidence.half.keys())
            ],
        }
        for room_id, evidence in sorted(layer.items())
    }


def layer_from_doc(doc: Any) -> Layer:
    """layer_to_doc's output back; ValueError when it isn't that shape."""
    if not isinstance(doc, dict):
        raise ValueError("layer must be an object")
    layer: Layer = {}
    for room_id, record in doc.items():
        if not isinstance(record, dict):
            raise ValueError("room evidence must be an object")
        seen = record.get("seen")
        votes = record.get("on")
        if not isinstance(seen, list) or not isinstance(votes, list):
            raise ValueError("room evidence needs seen and on lists")
        evidence = RoomEvidence(seen={_int(day) for day in seen})
        for item in votes:
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError("a vote is [date, full, half]")
            day, full, half = (_int(value) for value in item)
            if full:
                evidence.full[day] = full
            if half:
                evidence.half[day] = half
        layer[str(room_id)] = evidence
    return layer


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


def signature(*parts: Iterable[Any]) -> str:
    """A short stable hash of what a model was built from."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(sorted(map(repr, part))).encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


# -- settings -------------------------------------------------------------------


@dataclass(frozen=True)
class HabitSettings:
    """What an admin set for room habits (kv now_habit_config).

    excluded holds the periods learning was off or paused, as [start, end]
    epoch seconds (end None while it lasts): their dates are never learned.
    reset_at is the last reset: nothing before it counts.
    """

    learning: bool = True
    paused: bool = False
    weekend_days: tuple[int, ...] = DEFAULT_WEEKEND_DAYS
    self_scheduled_entity_ids: tuple[str, ...] = ()
    reset_at: int | None = None
    excluded: tuple[tuple[int, int | None], ...] = ()

    @classmethod
    def from_doc(cls, doc: Any) -> HabitSettings:
        """Stored settings; a missing or malformed field keeps its default."""
        if not isinstance(doc, dict):
            return cls()
        defaults = cls()
        values: dict[str, Any] = {}
        for name in ("learning", "paused"):
            if isinstance(doc.get(name), bool):
                values[name] = doc[name]
        try:
            values["weekend_days"] = _weekend_days(doc.get("weekend_days"))
        except HabitSettingsError:
            values["weekend_days"] = defaults.weekend_days
        ids = doc.get("self_scheduled_entity_ids")
        if isinstance(ids, list):
            values["self_scheduled_entity_ids"] = tuple(
                dict.fromkeys(i for i in ids if isinstance(i, str))
            )
        reset_at = doc.get("reset_at")
        if _is_time(reset_at):
            values["reset_at"] = reset_at
        excluded = doc.get("excluded")
        if isinstance(excluded, list):
            values["excluded"] = tuple(
                (item[0], item[1])
                for item in excluded
                if isinstance(item, list)
                and len(item) == 2
                and _is_time(item[0])
                and (item[1] is None or _is_time(item[1]))
            )
        return cls(**values)

    def to_doc(self) -> dict[str, Any]:
        return {
            "learning": self.learning,
            "paused": self.paused,
            "weekend_days": list(self.weekend_days),
            "self_scheduled_entity_ids": list(self.self_scheduled_entity_ids),
            "reset_at": self.reset_at,
            "excluded": [list(item) for item in self.excluded],
        }

    @property
    def status(self) -> str:
        """off, paused or learning (the learner tells warming_up apart)."""
        if not self.learning:
            return STATUS_OFF
        return STATUS_PAUSED if self.paused else STATUS_LEARNING

    def updated(
        self, payload: Any, now_t: int, *, managed_ids: Collection[str]
    ) -> HabitSettings:
        """The settings with an admin's change applied, or HabitSettingsError.

        managed_ids are the devices the hub manages (imported controls); a
        device must be one to be marked as running on its own schedule.
        """
        if not isinstance(payload, dict):
            raise HabitSettingsError("Body must be a JSON object")
        unknown = set(payload) - _SETTINGS_FIELDS
        if unknown:
            raise HabitSettingsError(f"Unknown habit setting: {sorted(unknown)[0]!r}")
        values: dict[str, Any] = {}
        for name in ("learning", "paused"):
            if name in payload:
                if not isinstance(payload[name], bool):
                    raise HabitSettingsError(f"{name} must be a boolean")
                values[name] = payload[name]
        if "weekend_days" in payload:
            values["weekend_days"] = _weekend_days(payload["weekend_days"])
        if "self_scheduled_entity_ids" in payload:
            ids = payload["self_scheduled_entity_ids"]
            if (
                not isinstance(ids, list)
                or len(ids) > MAX_SELF_SCHEDULED
                or any(not isinstance(i, str) for i in ids)
            ):
                raise HabitSettingsError(
                    "self_scheduled_entity_ids must be a list of at most "
                    f"{MAX_SELF_SCHEDULED} entity ids"
                )
            for entity_id in ids:
                if entity_id not in managed_ids:
                    raise HabitSettingsError(
                        f"{entity_id!r} is not a device the hub manages"
                    )
            values["self_scheduled_entity_ids"] = tuple(dict.fromkeys(ids))
        changed = replace(self, **values)
        return changed._with_excluded(self, now_t)

    def reset(self, now_t: int) -> HabitSettings:
        """Forget everything learned before now_t (pause/off periods too)."""
        return replace(self, reset_at=now_t, excluded=()).with_open_period(now_t)

    def with_open_period(self, now_t: int) -> HabitSettings:
        """Open an excluded period at now_t while learning is off or paused."""
        if self.status == STATUS_LEARNING or (
            self.excluded and self.excluded[-1][1] is None
        ):
            return self
        return replace(self, excluded=(*self.excluded, (now_t, None)))

    def _with_excluded(self, before: HabitSettings, now_t: int) -> HabitSettings:
        """Open or close the excluded period as learning stops or starts."""
        was_learning = before.status == STATUS_LEARNING
        learning = self.status == STATUS_LEARNING
        excluded = list(self.excluded)
        if was_learning and not learning:
            excluded.append((now_t, None))
        elif learning and excluded and excluded[-1][1] is None:
            excluded[-1] = (excluded[-1][0], now_t)
        # Periods that ended before the window can't matter any more.
        oldest = now_t - (WINDOW_DAYS + 2) * 86400
        excluded = [p for p in excluded if p[1] is None or p[1] >= oldest]
        return replace(self, excluded=tuple(excluded[-_MAX_EXCLUDED:]))

    def usable_days(self, today: int, utc_offset_min: int) -> UsableDays:
        """The local dates the model may learn from, for today."""
        excluded: set[int] = set()
        for start, end in self.excluded:
            first = max(local_day(start, utc_offset_min), today - WINDOW_DAYS)
            last = today if end is None else local_day(end, utc_offset_min)
            excluded.update(range(first, last + 1))
        first = today - WINDOW_DAYS
        if self.reset_at is not None:
            first = max(first, local_day(self.reset_at, utc_offset_min) + 1)
        return UsableDays(first, today - 1, frozenset(excluded))

    def model_key(self) -> tuple:
        """What in the settings changes the learned model (weekend days don't)."""
        return (self.self_scheduled_entity_ids, self.reset_at, self.excluded)


@dataclass(frozen=True)
class UsableDays:
    """The window's dates, first to last, minus the excluded ones."""

    first: int
    last: int
    excluded: frozenset[int]

    def __call__(self, day: int) -> bool:
        return self.first <= day <= self.last and day not in self.excluded


def _weekend_days(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6
        for day in value
    ):
        raise HabitSettingsError(
            "weekend_days must be a list of weekdays, Monday = 0 to Sunday = 6"
        )
    return tuple(sorted(set(value)))


def _is_time(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
