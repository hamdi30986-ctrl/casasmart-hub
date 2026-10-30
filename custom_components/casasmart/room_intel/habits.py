"""Room habits: when each room is usually switched on, learned from the log.

The room activity log (room_intel.activity, storage.RoomActivityTable) is the
only source, never Home Assistant's recorder. room_intel.learner builds the
model on the hub once a local day and keeps it in kv now_room_habits; the
all-off ranking asks it which rooms are usually on around now, and the
generated room suggestions which of a room's devices usually come on then,
and which are usually switched off by then. Nothing here imports Home
Assistant.

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
observed dates. A device usually comes on around now by the same rule over
its own votes (and its room's observed dates); a room's votes are its
devices' together.

Offs. A confirmed "off" of a counted device votes the same way, for that
device only (never for its room: a room's strength is its "on"s), in the
same layers with the same weights. A device is usually off by now by the
same rule over its off votes, read for "by now", not "around now":

    x_d = 0 when it was switched on at h (it was in use then), else
          max(v_off(d, h), v_off(d, h-1))

so an off this hour or the hour before counts in full, and an off an hour
later never does: the suggestion comes once the usual time has come, not
before it.

Ramadan (no switch, detected by itself). The dates of
Ramadan by the Umm al-Qura calendar (room_intel.hijri) are a profile of their
own, learned apart from the rest of the year (the regular profile). Each
profile has its own window and its own clock: its WINDOW_DAYS most recent
dates before today, an age counted in its own dates only. So the regular
model doesn't learn or age during Ramadan: on 1 Shawwal it is exactly the
model it was on 1 Ramadan. And the Ramadan model doesn't age between one
Ramadan and the next: next year's first day starts from this year's
Ramadan, which newer Ramadan days then outweigh. A Ramadan window reaches
back no further than the log keeps human rows (HUMAN_RETENTION_DAYS): last
year's Ramadan, never the one before, so nothing anyone did is learned from
for longer than the log keeps it. While it is Ramadan the ranking asks the
Ramadan profile, else the regular one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .activity import (
    HUMAN_RETENTION_DAYS,
    SOURCE_APP,
    SOURCE_HA_USER,
    SOURCE_SCENE,
    SOURCE_WALL_SWITCH,
)
from .hijri import ramadan_of, ramadans_before
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
# HabitScorer.any_habit answers a model keeps (askers and scopes in a day).
_MAX_ANSWERS = 64

LAYER_HOUSEHOLD = "household"
LAYER_PERSONAL = "personal"
LAYERS = (LAYER_HOUSEHOLD, LAYER_PERSONAL)

STATUS_OFF = "off"
STATUS_PAUSED = "paused"
STATUS_WARMING_UP = "warming_up"
STATUS_LEARNING = "learning"
STATUS_UNAVAILABLE = "unavailable"

# The profiles: Ramadan's dates, and every other date. The model docs carry
# the profile in their key so the two never mix.
PROFILE_REGULAR = "regular"
PROFILE_RAMADAN = "ramadan"
PROFILES = (PROFILE_REGULAR, PROFILE_RAMADAN)
# 2: wall switches vote at full weight (a saved v1 model has them at half);
# ages counted in the profile's own dates. 3: votes kept per device (a room's
# are its devices' together). 4: each device's off votes too.
MODEL_VERSION = 4
# Off and paused periods are kept while a date in them may still be read:
# the log's human rows live HUMAN_RETENTION_DAYS (a finished Ramadan is
# copied out of the log when it ends, learner.HabitLearner).
_EXCLUDED_HORIZON_DAYS = HUMAN_RETENTION_DAYS + 2

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


def profile_of(day: int) -> str:
    """The profile a local date belongs to: ramadan or regular."""
    return PROFILE_RAMADAN if ramadan_of(day) is not None else PROFILE_REGULAR


def profile_window(today: int, profile: str) -> list[int]:
    """The profile's last WINDOW_DAYS dates before today, newest first.

    Regular dates step over every Ramadan; Ramadan dates step back from this
    Ramadan to last year's, no further back than HUMAN_RETENTION_DAYS.
    """
    dates: list[int] = []
    if profile == PROFILE_RAMADAN:
        oldest = today - HUMAN_RETENTION_DAYS
        for month in ramadans_before(today):
            if month.last < oldest:
                break
            for day in range(
                min(month.last, today - 1), max(month.first, oldest) - 1, -1
            ):
                dates.append(day)
                if len(dates) == WINDOW_DAYS:
                    return dates
        return dates
    day = today - 1
    while len(dates) < WINDOW_DAYS:
        month = ramadan_of(day)
        if month is None:
            dates.append(day)
            day -= 1
        else:
            day = month.first - 1
    return dates


# -- the model ------------------------------------------------------------------


@dataclass
class Votes:
    """Votes by date: full and half each date's mask of hours (bit h) with a
    full or a half vote."""

    full: dict[int, int] = field(default_factory=dict)
    half: dict[int, int] = field(default_factory=dict)

    def add(self, day: int, on_hours: int, *, full: bool) -> None:
        votes = self.full if full else self.half
        votes[day] = votes.get(day, 0) | on_hours

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
        """The dates with a full vote."""
        return {day for day, mask in self.full.items() if mask}


@dataclass(frozen=True)
class OffVotes:
    """A device's off votes, read against its on votes for "usually off by
    now" (strength's evidence, like Votes)."""

    off: Votes
    on: Votes | None = None

    def near(self, day: int, hour: int, *, half_weight: bool) -> float:
        """x_d: the off vote at the hour or the hour before, 0 on a date it
        was switched on at the hour."""
        if self.on is not None and self.on.vote(day, hour, half_weight=half_weight):
            return 0.0
        before = (day, hour - 1) if hour else (day - 1, 23)
        return max(
            self.off.vote(day, hour, half_weight=half_weight),
            self.off.vote(*before, half_weight=half_weight),
        )


@dataclass
class RoomEvidence(Votes):
    """One room's votes in one layer: its devices' "on"s together.

    seen holds the observed dates; devices each device's own on votes, offs
    its off votes (which are no vote for the room).
    """

    seen: set[int] = field(default_factory=set)
    devices: dict[str, Votes] = field(default_factory=dict)
    offs: dict[str, Votes] = field(default_factory=dict)

    def add_device(
        self, entity_id: str, day: int, on_hours: int, *, full: bool
    ) -> None:
        """A device's vote, which is its room's too."""
        self.add(day, on_hours, full=full)
        self.devices.setdefault(entity_id, Votes()).add(day, on_hours, full=full)

    def add_device_off(
        self, entity_id: str, day: int, off_hours: int, *, full: bool
    ) -> None:
        """A device's off vote (its own only)."""
        self.offs.setdefault(entity_id, Votes()).add(day, off_hours, full=full)

    def off_votes(self) -> dict[str, OffVotes]:
        """Each device's off votes, read against its on votes."""
        return {
            entity_id: OffVotes(off, self.devices.get(entity_id))
            for entity_id, off in self.offs.items()
        }


# room_id -> evidence
Layer = dict[str, RoomEvidence]


@dataclass
class HabitModel:
    """Every layer, for the dates of one window.

    ages maps each of the window's dates to its age in the profile's own
    dates (UsableDays.ages); None ages a date by the calendar (today - date).
    """

    household: Layer = field(default_factory=dict)
    members: dict[str, Layer] = field(default_factory=dict)
    ages: Mapping[int, int] | None = None
    # HabitScorer.any_habit's answers: the model is a day's, and so are they.
    answers: dict[tuple, bool] = field(default_factory=dict, compare=False, repr=False)


def build_model(
    rows: Iterable[Mapping[str, Any]],
    *,
    counted: Collection[str],
    self_scheduled: Collection[str],
    usable: Callable[[int], bool],
) -> HabitModel:
    """Build the layers from RoomActivityTable.habit_rows rows.

    counted are the devices whose "on" makes a room on (and whose "off"
    votes for the device); self_scheduled the
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
        off_hours = row.get("off_hours", 0) if entity_id in counted else 0
        household = model.household.setdefault(room_id, RoomEvidence())
        household.seen.add(day)
        if on_hours:
            household.add_device(entity_id, day, on_hours, full=weight >= 1.0)
        if off_hours:
            household.add_device_off(entity_id, day, off_hours, full=weight >= 1.0)
        member_id = row["member_id"]
        if source == PERSONAL_SOURCE and member_id:
            own = model.members.setdefault(member_id, {}).setdefault(
                room_id, RoomEvidence()
            )
            own.seen.add(day)
            if on_hours:
                own.add_device(entity_id, day, on_hours, full=True)
            if off_hours:
                own.add_device_off(entity_id, day, off_hours, full=True)
    return model


def strength(
    evidence: Votes | OffVotes | None,
    observed: Iterable[int],
    *,
    hour: int,
    today: int,
    half_weight: bool = True,
    ages: Mapping[int, int] | None = None,
) -> tuple[float, int]:
    """(habit, dates with evidence) for a room at an hour, over observed dates.

    ages, when given, ages each date in its profile's own dates.
    """
    weighted = total = 0.0
    dates = 0
    for day in observed:
        age = today - day if ages is None else ages.get(day, today - day)
        weight = 2.0 ** (-age / HALF_LIFE_DAYS)
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
        self._rooms = rooms
        self._model = model
        self._today = today
        self._hour = hour
        self._weekend_days = tuple(sorted(weekend_days))
        self._member_id = member_id
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
        return self._blend(
            self._model.household.get(room_id),
            self._member.get(room_id) if self.personal_weight else None,
        )

    def _blend(
        self,
        household: Votes | OffVotes | None,
        mine: Votes | OffVotes | None,
        hour: int | None = None,
    ) -> tuple[float, int]:
        """(strength, household dates with evidence) of these votes at this
        hour (None: the scorer's): the household's, with the member's own
        blended over them."""
        hour = self._hour if hour is None else hour
        habit, dates = strength(
            household,
            self._observed,
            hour=hour,
            today=self._today,
            ages=self._model.ages,
        )
        weight = self.personal_weight
        if weight:
            own, _ = strength(
                mine,
                self._own_observed,
                hour=hour,
                today=self._today,
                half_weight=False,
                ages=self._model.ages,
            )
            habit = weight * own + (1 - weight) * habit
        return habit, dates

    def learned(self, room: Mapping[str, Any]) -> tuple[str, dict, tuple] | None:
        """The ranking's learned tier for a room: habit_now, or None."""
        if not self.ready:
            return None
        habit, dates = self.score(room["room_id"])
        if habit < MIN_STRENGTH or dates < MIN_EVIDENCE_DAYS:
            return None
        return REASON_HABIT_NOW, {}, (LEARNED_HABIT, -round(habit, 4))

    def usual_devices(self, room_id: str) -> list[str]:
        """The room's devices that usually come on around now, strongest
        first (ties by entity id).

        A device does when its own votes, blended like its room's (score),
        reach MIN_STRENGTH on at least MIN_EVIDENCE_DAYS dates. None until
        the asker's rooms are ready.
        """
        return self._usual(room_id, lambda evidence: evidence.devices)

    def usual_off_devices(self, room_id: str) -> list[str]:
        """The room's devices that are usually switched off by now, strongest
        first (ties by entity id): usual_devices' rule over their off votes
        (OffVotes)."""
        return self._usual(room_id, RoomEvidence.off_votes)

    def any_habit(
        self,
        on: Mapping[str, Collection[str]],
        off: Mapping[str, Collection[str]],
    ) -> bool:
        """True once the asker's habits have learned something a habit plan
        can switch: at some hour of today's day class, one of a room's
        devices in on (room id -> entity ids) usually comes on then
        (usual_devices' bar), or one in off is usually off by then
        (usual_off_devices'). False until ready.

        Every hour, so the answer holds all day; in the asker's layer (the
        household's, or a member's blend), so a phone is "learned" only once
        its own blend can build a plan. Kept on the model (a day's).
        """
        if not self.ready:
            return False
        key = (
            self._today,
            self._weekend_days,
            self._member_id,
            tuple(self._rooms),
            frozenset((r, e) for r, ids in on.items() for e in ids),
            frozenset((r, e) for r, ids in off.items() for e in ids),
        )
        answers = self._model.answers
        if key not in answers:
            if len(answers) >= _MAX_ANSWERS:
                answers.clear()
            answers[key] = self._any_habit(on, off)
        return answers[key]

    def _any_habit(
        self,
        on: Mapping[str, Collection[str]],
        off: Mapping[str, Collection[str]],
    ) -> bool:
        for room_id in self._rooms:
            household = self._model.household.get(room_id)
            if household is None:
                continue  # a device needs household dates with evidence
            own = self._member.get(room_id) if self.personal_weight else None
            for wanted, votes_of in (
                (on.get(room_id, ()), lambda evidence: evidence.devices),
                (off.get(room_id, ()), RoomEvidence.off_votes),
            ):
                if not wanted:
                    continue
                theirs = votes_of(household)
                mine = votes_of(own) if own else {}
                for entity_id in wanted:
                    if entity_id not in theirs:
                        continue
                    for hour in range(24):
                        habit, dates = self._blend(
                            theirs[entity_id], mine.get(entity_id), hour
                        )
                        if habit >= MIN_STRENGTH and dates >= MIN_EVIDENCE_DAYS:
                            return True
        return False

    def _usual(
        self,
        room_id: str,
        votes_of: Callable[[RoomEvidence], Mapping[str, Votes | OffVotes]],
    ) -> list[str]:
        """The devices whose votes (votes_of a room's evidence, by entity id)
        pass the bar, strongest first."""
        if not self.ready:
            return []
        household = self._model.household.get(room_id)
        own = self._member.get(room_id) if self.personal_weight else None
        theirs = votes_of(household) if household else {}
        mine = votes_of(own) if own else {}
        passing = []
        for entity_id in theirs.keys() | mine.keys():
            habit, dates = self._blend(theirs.get(entity_id), mine.get(entity_id))
            if habit >= MIN_STRENGTH and dates >= MIN_EVIDENCE_DAYS:
                passing.append((-round(habit, 4), entity_id))
        return [entity_id for _, entity_id in sorted(passing)]


# -- storage shape --------------------------------------------------------------


def layer_to_doc(layer: Layer) -> dict[str, Any]:
    """A layer as JSON: per room, observed dates and each device's
    [date, full, half] on votes (the room's are theirs together) and off
    votes."""

    def votes_doc(devices: Mapping[str, Votes]) -> dict[str, list[list[int]]]:
        return {
            entity_id: [
                [day, votes.full.get(day, 0), votes.half.get(day, 0)]
                for day in sorted(votes.full.keys() | votes.half.keys())
            ]
            for entity_id, votes in sorted(devices.items())
        }

    return {
        room_id: {
            "seen": sorted(evidence.seen),
            "devices": votes_doc(evidence.devices),
            "offs": votes_doc(evidence.offs),
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
        devices = record.get("devices")
        offs = record.get("offs")
        if (
            not isinstance(seen, list)
            or not isinstance(devices, dict)
            or not isinstance(offs, dict)
        ):
            raise ValueError("room evidence needs a seen list, devices and offs")
        evidence = RoomEvidence(seen={_int(day) for day in seen})
        for votes_by_device, add in (
            (devices, evidence.add_device),
            (offs, evidence.add_device_off),
        ):
            for entity_id, votes in votes_by_device.items():
                if not isinstance(votes, list):
                    raise ValueError("a device's votes must be a list")
                for item in votes:
                    if not isinstance(item, list) or len(item) != 3:
                        raise ValueError("a vote is [date, full, half]")
                    day, full, half = (_int(value) for value in item)
                    if full:
                        add(str(entity_id), day, full, full=True)
                    if half:
                        add(str(entity_id), day, half, full=False)
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
        # Periods that ended before any date still read can't matter any more.
        oldest = now_t - _EXCLUDED_HORIZON_DAYS * 86400
        excluded = [p for p in excluded if p[1] is None or p[1] >= oldest]
        return replace(self, excluded=tuple(excluded[-_MAX_EXCLUDED:]))

    def usable_days(
        self, today: int, utc_offset_min: int, profile: str | None = None
    ) -> UsableDays:
        """The local dates a profile's model may learn from, for today.

        profile None is today's (Ramadan or regular).
        """
        profile = profile_of(today) if profile is None else profile
        window = profile_window(today, profile)
        ages = {day: age for age, day in enumerate(window, start=1)}
        if not window:
            return UsableDays(today, today - 1, frozenset(), ages, profile)
        first = window[-1]
        if self.reset_at is not None:
            first = max(first, local_day(self.reset_at, utc_offset_min) + 1)
        return UsableDays(
            first,
            window[0],
            frozenset(self.blocked_days(window, utc_offset_min)),
            ages,
            profile,
        )

    def blocked_days(self, days: Iterable[int], utc_offset_min: int) -> set[int]:
        """The dates among days that may never be learned: touched by an off
        or paused period, or not after the last reset's date."""
        reset_day = (
            None if self.reset_at is None else local_day(self.reset_at, utc_offset_min)
        )
        periods = [
            (
                local_day(start, utc_offset_min),
                None if end is None else local_day(end, utc_offset_min),
            )
            for start, end in self.excluded
        ]
        return {
            day
            for day in days
            if (reset_day is not None and day <= reset_day)
            or any(
                first <= day and (last is None or day <= last)
                for first, last in periods
            )
        }

    def model_key(self) -> tuple:
        """What in the settings changes the learned model (weekend days don't)."""
        return (self.self_scheduled_entity_ids, self.reset_at, self.excluded)


@dataclass(frozen=True)
class UsableDays:
    """One profile's window: its dates from first to last, minus the
    excluded ones.

    ages maps every date of the window (excluded ones too) to its age in the
    profile's own dates, 1 for the newest; first is the oldest date that may
    be learned (after a reset), last the newest.
    """

    first: int
    last: int
    excluded: frozenset[int]
    ages: Mapping[int, int] = field(default_factory=dict)
    profile: str = PROFILE_REGULAR

    def __call__(self, day: int) -> bool:
        return (
            self.first <= day <= self.last
            and day in self.ages
            and day not in self.excluded
        )

    def spans(self) -> list[tuple[int, int]]:
        """The learnable dates as runs of consecutive dates, oldest first."""
        runs: list[tuple[int, int]] = []
        for day in sorted(d for d in self.ages if self(d)):
            if runs and runs[-1][1] == day - 1:
                runs[-1] = (runs[-1][0], day)
            else:
                runs.append((day, day))
        return runs


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
