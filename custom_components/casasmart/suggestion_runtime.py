"""Suggestions over live Home Assistant state, and change notifications.

SuggestionRuntime feeds the rules in suggestions with live states, scenes,
the home time zone and sunset, and answers the suggestion endpoints. It
watches the entities its rules use and fires EVENT_SUGGESTIONS_CHANGED (with
no payload) when the offer changes, so WebSocket clients refetch; timers
re-evaluate at the next window boundary and when a snooze ends.
GeneratedSuggestionRuntime does the same for the generated room scenes.
Neither runs a scene; only an explicit run request does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.sun import get_astral_event_date

from .const import (
    EVENT_ENERGY_CHANGED,
    EVENT_REGISTRY_CHANGED,
    EVENT_SUGGESTIONS_CHANGED,
)
from .filtering import in_scope, is_served
from .generated_suggestions import (
    HABIT_KINDS,
    KIND_ROOM_ECO,
    KIND_ROOM_OFF,
    KIND_USUALLY_OFF_NOW,
    KIND_USUALLY_ON_NOW,
    KINDS,
    USUAL_ROOMS,
    Ask,
    digest,
    make_suggestion,
    make_usual_off_suggestion,
    make_usual_suggestion,
    window,
)
from .now_data import AC_ON_MODES, KIND_AC, is_room_ac, room_light_kind, room_on_kind
from .room_intel.off_rooms import (
    JUST_OFF_WINDOW_SECONDS,
    REASON_HABIT_NOW,
    changed_at,
)
from .room_intel.ranking import async_rank_rooms, imported_controls
from .runtime_lookup import loaded_runtime_data
from .storage import StorageError
from .suggestions import SuggestionError, evaluate, next_boundary, state_value

_LOGGER = logging.getLogger(__name__)

# A burst of changes becomes one refresh this long after the last of them.
_DEBOUNCE_SECONDS = 0.2
# The shortest timer delay, and the retry delay after a failed refresh.
_MIN_DELAY_SECONDS = 0.05
_RETRY_SECONDS = 60


class SuggestionRuntime:
    """Rule-based suggestions over live HA state.

    With now_data it also builds its generated counterpart.
    """

    def __init__(
        self, hass, store, registry, *, clock=None, sunset=None, now_data=None
    ):
        self.hass, self.store, self.registry = hass, store, registry
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sunset = sunset or self._sunset
        self._unsubs = []
        self._state_unsub = None
        self._ids = set()
        self._timer = self._debounce = self._task = None
        self._stopped = False
        self._fingerprint = None
        self._lock = asyncio.Lock()
        self.generated = (
            GeneratedSuggestionRuntime(hass, store, registry, now_data, clock=clock)
            if now_data is not None
            else None
        )

    def _sunset(self, day):
        """Sunset at the home on the day, or None without a working sun.sun."""
        sun = self.hass.states.get("sun.sun")
        if state_value(sun) not in {"above_horizon", "below_horizon"}:
            return None
        return get_astral_event_date(self.hass, "sunset", day)

    async def context(self):
        """Everything one evaluation needs, read once.

        Returns (document, scenes by id, states, now, zone). Only the entities
        the enabled rules refer to are read.
        """
        data, scenes = await self.hass.async_add_executor_job(
            lambda: (self.store.snapshot(), self.registry.list_scenes())
        )
        scenes = {s["scene_id"]: s for s in scenes}
        ids = set()
        for rule in data["rules"]:
            if not rule["enabled"]:
                continue
            ids.update(c["entity_id"] for c in rule["conditions"])
            ids.update(
                i["entity_id"]
                for i in scenes.get(rule["scene_id"], {}).get("entities", [])
            )
            if rule["window"]["kind"] == "sunset":
                ids.add("sun.sun")
        states = {eid: self.hass.states.get(eid) for eid in ids}
        return data, scenes, states, self.clock(), ZoneInfo(self.hass.config.time_zone)

    def visible(self, scope):
        """A predicate: is this entity served and inside the scope?"""
        return lambda eid: is_served(self.hass, eid) and in_scope(self.hass, eid, scope)

    def candidates(self, context, scope, *, policy_checks=True):
        """Yield (rule, suggestion or None, reason) by priority, then id."""
        data, scenes, states, now, zone = context
        visible = self.visible(scope)
        for rule in sorted(data["rules"], key=lambda r: (-r["priority"], r["rule_id"])):
            suggestion, reason = evaluate(
                rule,
                scenes.get(rule["scene_id"]),
                states,
                now,
                zone,
                self.sunset,
                visible,
                policy_checks=policy_checks,
            )
            yield rule, suggestion, reason

    def payload_from(self, context, member, scope):
        """The GET answer: the first offer this member has not suppressed.

        An offer that ran, is running or may have run is skipped; one whose
        run partly failed stays offered with that receipt attached.
        """
        data, _, _, now, _ = context
        for _, suggestion, _ in self.candidates(context, scope):
            if suggestion is None:
                continue
            oid = suggestion["occurrence_id"]
            suppression = data["suppressions"].get(
                self.store.suppression_key(member, oid)
            )
            receipt = data["executions"].get(oid)
            if suppression and datetime.fromisoformat(suppression["until"]) > now:
                continue
            if receipt and receipt["status"] in {"succeeded", "executing", "unknown"}:
                continue
            if receipt:
                suggestion["last_execution"] = receipt
            return {
                "version": 1,
                "status": "available",
                "suggestion": suggestion,
                "refresh_at": next_boundary(
                    data["rules"], now, context[4], self.sunset
                ).isoformat(),
            }
        return {
            "version": 1,
            "status": "no_match" if data["rules"] else "not_configured",
            "suggestion": None,
            "refresh_at": next_boundary(
                data["rules"], now, context[4], self.sunset
            ).isoformat(),
        }

    async def payload(self, member, scope):
        """payload_from on a fresh context; "unavailable" if it can't be read."""
        try:
            return self.payload_from(await self.context(), member, scope)
        except (StorageError, sqlite3.Error, SuggestionError, ZoneInfoNotFoundError):
            _LOGGER.warning("Suggestion snapshot unavailable")
            return {"version": 1, "status": "unavailable", "suggestion": None}

    async def start(self):
        """Listen for registry, energy and HA config changes, then refresh."""
        if self.generated:
            await self.generated.start()
        for event in (
            EVENT_REGISTRY_CHANGED,
            EVENT_ENERGY_CHANGED,
            "core_config_updated",
        ):
            self._unsubs.append(self.hass.bus.async_listen(event, self._changed))
        await self.refresh()

    @callback
    def _changed(self, _event=None):
        """Coalesce a burst of changes into one refresh."""
        if self._stopped:
            return
        if self._debounce:
            self._debounce.cancel()
        self._debounce = self.hass.loop.call_later(_DEBOUNCE_SECONDS, self._kick)

    @callback
    def _kick(self):
        self._debounce = None
        if not self._stopped:
            self._task = self.hass.async_create_task(self.refresh())

    async def refresh(self):
        """Re-evaluate, re-subscribe, notify on change, and re-arm the timer.

        Clients are notified only when the offer set or the live
        suppressions differ from the last refresh. A failed refresh tries
        again in a minute.
        """
        async with self._lock:
            if self._stopped:
                return
            try:
                context = await self.refresh_context()
                if self._stopped:
                    return
                data, _, states, now, zone = context
                ids = set(states)
                if ids != self._ids:
                    if self._state_unsub:
                        self._state_unsub()
                    self._state_unsub = (
                        async_track_state_change_event(
                            self.hass, list(ids), self._changed
                        )
                        if ids
                        else None
                    )
                    self._ids = ids
                eligible = [s for _, s, _ in self.candidates(context, None) if s]
                for suggestion in eligible:
                    suggestion.pop("generated_at", None)
                active_data = {
                    **data,
                    "suppressions": {
                        k: v
                        for k, v in data["suppressions"].items()
                        if datetime.fromisoformat(v["until"]) > now
                    },
                }
                fingerprint = json.dumps([active_data, eligible], sort_keys=True)
                if fingerprint != self._fingerprint:
                    self._fingerprint = fingerprint
                    self.hass.bus.async_fire(EVENT_SUGGESTIONS_CHANGED)
                delay = self._seconds_to_boundary(data, now, zone)
                # Wake when a snooze ends, even if nothing else changes.
                for suppression in data["suppressions"].values():
                    seconds = (
                        datetime.fromisoformat(suppression["until"]) - now
                    ).total_seconds()
                    if seconds > 0:
                        delay = min(delay, seconds)
            except Exception:
                _LOGGER.exception("Suggestion refresh failed")
                delay = _RETRY_SECONDS
            if self._timer:
                self._timer.cancel()
            if not self._stopped:
                self._timer = self.hass.loop.call_later(delay, self._kick)

    async def refresh_context(self):
        """The context a refresh evaluates (every enabled rule)."""
        return await self.context()

    def _seconds_to_boundary(self, data, now, zone):
        """Seconds until the next window opens or closes."""
        return max(
            _MIN_DELAY_SECONDS,
            (
                next_boundary(data["rules"], now, zone, self.sunset) - now
            ).total_seconds(),
        )

    def stop(self):
        """Cancel every listener, timer and pending refresh."""
        if self.generated:
            self.generated.stop()
        self._stopped = True
        for cancel in self._unsubs:
            cancel()
        self._unsubs.clear()
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        for handle in (self._timer, self._debounce, self._task):
            if handle:
                handle.cancel()


class GeneratedSuggestionRuntime(SuggestionRuntime):
    """Generated room scenes, served on their own endpoint.

    Clients opt in with the generated_room_suggestions_v1 capability, to
    usually_on_now with ?kinds= (generated_room_habit_suggestions_v1), to
    usually_off_now the same way (generated_room_habit_off_suggestions_v1)
    and to the habit kinds replacing the others once the habits are ready
    with ?habits_only=1 (generated_room_habits_only_v1).
    """

    def __init__(self, hass, store, registry, now_data, *, clock=None):
        super().__init__(hass, store, registry, clock=clock)
        self.now_data = now_data

    async def room_context(self, scope):
        """The caller's rooms, busiest first (room_intel.ranking).

        Returns (ranked rooms, states by room, states by entity).
        """
        return await async_rank_rooms(self.hass, self.registry, self.now_data, scope)

    def _read_stores(self):
        rooms = self.registry.list_rooms()
        policies = {
            r["room_id"]: self.now_data.room_policy(r["room_id"]) for r in rooms
        }
        return rooms, policies, self.registry.list_user_devices()

    async def context(self, scope=None, ask=None, member_id=None):
        """Plan this window's room scenes for the scope.

        The two busiest rooms are fixed for the two-hour window (persisted,
        so a restart keeps them); the first gets an off and an eco plan,
        the second an off plan. Asked for usually_on_now (ask: the kinds,
        and the household's habits or member_id's), up to USUAL_ROOMS rooms
        that are off but usually on around now get an on plan each, unless
        an Energy Saving level is active. Asked for usually_off_now, up to
        USUAL_ROOMS rooms that are on get an off plan each for their devices
        usually switched off by now, Energy Saving or not. Asked habits_only,
        only the habit kinds once the caller's habits are ready
        (_habits_ready), and only the others before then. Returns the same
        tuple shape as the rule runtime, with the plans standing in for
        rules and scenes.
        """
        ask = ask or Ask()
        now = self.clock()
        runtime = learner = None
        habit_kinds = ask.kinds & HABIT_KINDS
        if habit_kinds or ask.habits_only:
            runtime = loaded_runtime_data(self.hass)
            learner = getattr(runtime, "room_habits", None)
            energy = getattr(runtime, "energy", None)
            if energy is not None and energy.active_level is not None:
                # Nothing is turned on while Energy Saving is on; off may be.
                habit_kinds -= {KIND_USUALLY_ON_NOW}
                if not habit_kinds and not ask.habits_only:
                    learner = None
        habits = stores = None
        if learner is not None:
            # Ranked as the Now page ranks for this caller, every off room
            # with its tier: usually_on_now goes only to a habit_now room.
            habit_member = None if ask.household else member_id
            habits = learner.query(habit_member)
            stores = await self.hass.async_add_executor_job(self._read_stores)
            ranked, grouped, states = await async_rank_rooms(
                self.hass,
                self.registry,
                self.now_data,
                scope,
                activity=getattr(runtime, "room_activity", None),
                member_id=habit_member,
                now_t=now.timestamp(),
                habits=habits,
                stores=stores,
                every_off_room=True,
            )
        else:
            ranked, grouped, states = await self.room_context(scope)
        plans = []
        if ask.kinds & {KIND_ROOM_OFF, KIND_ROOM_ECO}:
            scope_key = digest(
                ["imported_controls_v1", sorted(scope) if scope is not None else None]
            )
            selected = await self.hass.async_add_executor_job(
                self.store.select_generated_rooms,
                scope_key,
                window(now)[0],
                [r["room_id"] for r in ranked if r["active_count"]],
            )
            rooms = {r["room_id"]: r for r in ranked}
            for index, rid in enumerate(selected):
                if rid not in rooms:
                    continue
                for kind in (
                    [KIND_ROOM_OFF, KIND_ROOM_ECO] if index == 0 else [KIND_ROOM_OFF]
                ):
                    if kind not in ask.kinds:
                        continue
                    plan = make_suggestion(
                        rooms[rid],
                        kind,
                        grouped[rid],
                        now,
                        temperature_unit=getattr(
                            getattr(self.hass.config, "units", None),
                            "temperature_unit",
                            "°C",
                        ),
                    )
                    if plan:
                        plans.append(plan)
        zone = ZoneInfo(self.hass.config.time_zone)
        wake_t = None
        if habits is not None and KIND_USUALLY_ON_NOW in habit_kinds:
            plans.extend(
                await self._usual_plans(ranked, grouped, stores, habits, now, zone)
            )
        if habits is not None and KIND_USUALLY_OFF_NOW in habit_kinds:
            off_plans, wake_t = self._usual_off_plans(
                ranked, grouped, stores, habits, now, zone
            )
            plans.extend(off_plans)
        ready = None
        if ask.habits_only:
            ready = habits is not None and await self._habits_ready(
                grouped, stores, habits
            )
            plans = [p for p in plans if (p["kind"] in HABIT_KINDS) == ready]
        data = await self.hass.async_add_executor_job(self.store.snapshot)
        data = {
            **data,
            "rules": [],
            "_generated_plans": plans,
            "_generated_habits": habits is not None,
            "_generated_wake_t": wake_t,
            "_generated_habits_ready": ready,
        }
        scenes = {
            p["scene_id"]: {
                **p["scene"],
                "entities": p["actions"],
                # Turning a room on never runs while Energy Saving is on.
                "works_during_energy_saving": p["kind"] != KIND_USUALLY_ON_NOW,
                "generated_room_v1": True,
                "room_id": p["room_id"],
                "kind": p["kind"],
            }
            for p in plans
        }
        return data, scenes, states, now, zone

    async def _habits_ready(self, grouped, stores, habits):
        """True once the caller's habits are ready for habit plans: past the
        warm-up, a device of a room that takes part in room commands passes
        the habit bar at some hour of today's day class, for coming on
        (what _usual_plans may turn on) or for being off by then (what
        _usual_off_plans may turn off) (HabitQuery.habits_ready). Every
        hour of every device the first time a day: in the executor."""
        _, policies, devices = stores
        gang_types = imported_controls(devices)
        on, off = {}, {}
        for room_id, states in grouped.items():
            policy = policies.get(room_id, {})
            if not policy.get("participates"):
                continue
            on[room_id] = [
                state.entity_id
                for state in states
                if room_light_kind(state, policy, gang_types) is not None
                or is_room_ac(state)
            ]
            off[room_id] = [
                state.entity_id
                for state in states
                if room_on_kind(state, policy, gang_types) is not None
            ]
        return await self.hass.async_add_executor_job(habits.habits_ready, on, off)

    async def _usual_plans(self, ranked, grouped, stores, habits, now, zone):
        """The usually_on_now plans, strongest habit first.

        Only a room the ranking calls usually on around now (habit_now: off,
        nothing still running, not just switched off) that takes part in
        room commands, with its usual devices that are off: the lights and
        fans one tap "on" may turn on (now_data.room_light_kind), and its
        ACs (is_room_ac, as they count in every room), each at the level
        last used around now (HabitQuery.async_levels; an AC only with one).
        """
        _, policies, devices = stores
        gang_types = imported_controls(devices)
        hour = now.astimezone(zone).hour
        plans = []
        for room in ranked:
            policy = policies.get(room["room_id"], {})
            if room["rank"]["reason"] != REASON_HABIT_NOW or not policy.get(
                "participates"
            ):
                continue
            usual = {
                state.entity_id: state
                for state in grouped[room["room_id"]]
                if room_light_kind(state, policy, gang_types) is not None
                or is_room_ac(state)
            }
            states = [
                usual[entity_id]
                for entity_id in habits.usual_devices(room["room_id"])
                if entity_id in usual and usual[entity_id].state == "off"
            ]
            if not states:
                continue
            levels = await habits.async_levels(
                room["room_id"], [state.entity_id for state in states], now.timestamp()
            )
            plan = make_usual_suggestion(
                room,
                states,
                now,
                layer=habits.summary["layer"],
                hour=hour,
                levels=levels,
            )
            if plan:
                plans.append(plan)
                if len(plans) == USUAL_ROOMS:
                    break
        return plans

    def _usual_off_plans(self, ranked, grouped, stores, habits, now, zone):
        """The usually_off_now plans, in the ranking's order (busiest first),
        and when a device held back as just switched on stops being so (epoch
        seconds; None: none is).

        Only a room that is on and takes part in room commands, with its
        devices that are on and usually switched off by now
        (HabitQuery.usual_off_devices): what makes the room on
        (now_data.room_on_kind: the lights, fans and wall relays room off
        switches, and its ACs), so never a plug, an outlet, a heater, a
        lock, a cover or a TV, nor a device left out of room activity. A
        device switched on (or into another mode) less than
        JUST_OFF_WINDOW_SECONDS ago is in use: left out until then.
        """
        _, policies, devices = stores
        gang_types = imported_controls(devices)
        hour = now.astimezone(zone).hour
        now_t = now.timestamp()
        plans = []
        wake_t = None
        for room in ranked:
            policy = policies.get(room["room_id"], {})
            if not room["rank"]["active"] or not policy.get("participates"):
                continue
            usual = habits.usual_off_devices(room["room_id"])
            if not usual:
                continue
            on = {}
            for state in grouped[room["room_id"]]:
                kind = room_on_kind(state, policy, gang_types)
                if kind is not None and (
                    state.state in AC_ON_MODES
                    if kind == KIND_AC
                    else state.state == "on"
                ):
                    on[state.entity_id] = state
            states = []
            for entity_id in usual:
                state = on.get(entity_id)
                if state is None:
                    continue
                changed = changed_at(state)
                if changed is not None and now_t < changed + JUST_OFF_WINDOW_SECONDS:
                    until = changed + JUST_OFF_WINDOW_SECONDS
                    wake_t = until if wake_t is None else min(wake_t, until)
                    continue
                states.append(state)
            plan = make_usual_off_suggestion(
                room, states, now, layer=habits.summary["layer"], hour=hour
            )
            if plan:
                plans.append(plan)
                if len(plans) == USUAL_ROOMS:
                    break
        return plans, wake_t

    async def refresh_context(self):
        """Every kind, on the household's habits: a change to what the shared
        tablet would be offered tells clients to refetch."""
        return await self.context(None, Ask(KINDS, household=True))

    def candidates(self, context, scope, *, policy_checks=True):
        """Yield each plan whose every device the caller can see."""
        visible = self.visible(scope)
        for plan in context[0]["_generated_plans"]:
            if all(visible(a["entity_id"]) for a in plan["actions"]):
                yield {}, dict(plan), "eligible"

    @staticmethod
    def _refresh_at(data, now, zone):
        """When the plans may change by themselves: the end of the two-hour
        window or, when habits made them, the next local hour if sooner (a
        habit is an hour's), or when a device held back from usually_off_now
        as just switched on stops being so, if sooner still."""
        end = window(now)[1]
        if not data["_generated_habits"]:
            return end
        hour = now.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        end = min(end, (hour + timedelta(hours=1)).astimezone(UTC))
        wake_t = data.get("_generated_wake_t")
        if wake_t is not None:
            end = min(end, datetime.fromtimestamp(wake_t, UTC))
        return end

    def _seconds_to_boundary(self, data, now, zone):
        """The rule boundary or _refresh_at, if sooner."""
        return min(
            super()._seconds_to_boundary(data, now, zone),
            max(
                _MIN_DELAY_SECONDS,
                (self._refresh_at(data, now, zone) - now).total_seconds(),
            ),
        )

    def payload_from(self, context, member, scope):
        """Every plan this member has not suppressed and nobody tried to run.

        Asked habits_only, the answer says whether the habits were ready
        (habits_ready).
        """
        data, _, _, now, zone = context

        def suppressed(slot):
            suppression = data["suppressions"].get(
                self.store.suppression_key(member, slot)
            )
            return bool(
                suppression and datetime.fromisoformat(suppression["until"]) > now
            )

        plans = []
        for _, plan, _ in self.candidates(context, scope):
            slot = plan["suppression_id"]
            if suppressed(slot):
                continue
            # Any attempted execution blocks a changed version of this slot too.
            receipts = [
                r
                for r in data["executions"].values()
                if r.get("suppression_id") == slot
            ]
            if receipts:
                continue
            plans.append(plan)
        payload = {
            "version": 1,
            "source": "generated_room_v1",
            "status": "available" if plans else "no_match",
            "suggestion": plans[0] if plans else None,
            "suggestions": plans,
            "refresh_at": self._refresh_at(data, now, zone).isoformat(),
        }
        if data["_generated_habits_ready"] is not None:
            payload["habits_ready"] = data["_generated_habits_ready"]
        return payload

    async def payload(self, member, scope, ask=None):
        """payload_from for the caller's scope and ask; "unavailable" on
        failure."""
        try:
            return self.payload_from(
                await self.context(scope, ask, member), member, scope
            )
        except (StorageError, sqlite3.Error, SuggestionError, ZoneInfoNotFoundError):
            return {
                "version": 1,
                "status": "unavailable",
                "suggestion": None,
                "suggestions": [],
            }
