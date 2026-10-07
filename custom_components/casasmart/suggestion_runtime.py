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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.sun import get_astral_event_date

from .const import (
    EVENT_ENERGY_CHANGED,
    EVENT_REGISTRY_CHANGED,
    EVENT_SUGGESTIONS_CHANGED,
)
from .filtering import area_id_of, in_scope, is_served
from .generated_suggestions import digest, make_suggestion, window
from .now_data import is_room_activity_candidate
from .storage import StorageError
from .suggestions import SuggestionError, evaluate, next_boundary, state_value

_LOGGER = logging.getLogger(__name__)


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
        """Coalesce a burst of changes into one refresh 0.2 s after the last."""
        if self._stopped:
            return
        if self._debounce:
            self._debounce.cancel()
        self._debounce = self.hass.loop.call_later(0.2, self._kick)

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
                context = await self.context()
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
                delay = max(
                    0.05,
                    (
                        next_boundary(data["rules"], now, zone, self.sunset) - now
                    ).total_seconds(),
                )
                if isinstance(self, GeneratedSuggestionRuntime):
                    delay = min(
                        delay, max(0.05, (window(now)[1] - now).total_seconds())
                    )
                # Wake when a snooze ends, even if nothing else changes.
                for suppression in data["suppressions"].values():
                    seconds = (
                        datetime.fromisoformat(suppression["until"]) - now
                    ).total_seconds()
                    if seconds > 0:
                        delay = min(delay, seconds)
            except Exception:
                _LOGGER.exception("Suggestion refresh failed")
                delay = 60
            if self._timer:
                self._timer.cancel()
            if not self._stopped:
                self._timer = self.hass.loop.call_later(delay, self._kick)

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

    Clients opt in with the generated_room_suggestions_v1 capability.
    """

    def __init__(self, hass, store, registry, now_data, *, clock=None):
        super().__init__(hass, store, registry, clock=clock)
        self.now_data = now_data

    async def room_context(self, scope):
        """Rank the caller's rooms by how many safe devices are on.

        Only devices imported into the registry count. When any room has an
        activity policy, only participating rooms and their approved devices
        count; otherwise every visible room does. Returns (ranked rooms,
        states by room, states by entity).
        """
        rooms, policies, devices = await self.hass.async_add_executor_job(
            lambda: (
                self.registry.list_rooms(),
                {
                    r["room_id"]: self.now_data.room_policy(r["room_id"])
                    for r in self.registry.list_rooms()
                },
                self.registry.list_user_devices(),
            )
        )
        rooms = [r for r in rooms if scope is None or r["room_id"] in scope]
        configured = any(
            policies.get(r["room_id"], {}).get("participates") for r in rooms
        )
        gang_types = {}
        control_ids = set()
        # Older registry records key gang types by these channel names
        # instead of by entity id.
        suffixes = (
            "left",
            "right",
            "center",
            "l1",
            "l2",
            "l3",
            "endpoint_1",
            "endpoint_2",
            "endpoint_3",
            "gang_1",
            "gang_2",
            "gang_3",
        )
        for device in devices:
            gangs = device.get("gangs", {})
            legacy_types = device.get("gang_types", {})
            config_ids = set(device.get("config_entity_ids", []))
            for eid in device.get("control_entity_ids", device.get("entity_ids", [])):
                gang = gangs.get(eid, {})
                if eid in config_ids or gang.get("presentation") == "hidden":
                    continue
                control_ids.add(eid)
                local = eid.split(".", 1)[-1]
                key = next(
                    (s for s in suffixes if local == s or local.endswith("_" + s)), eid
                )
                gang_types[eid] = gang.get("type", legacy_types.get(key))
        visible = self.visible(scope)
        states = {
            s.entity_id: s
            for s in self.hass.states.async_all()
            if s.entity_id in control_ids and visible(s.entity_id)
        }
        grouped = {r["room_id"]: [] for r in rooms}
        for state in states.values():
            rid = area_id_of(self.hass, state.entity_id)
            if rid in grouped:
                grouped[rid].append(state)
        ranked = []
        for room in rooms:
            rid = room["room_id"]
            policy = policies.get(rid, {})
            if configured and not policy.get("participates"):
                continue
            eligible = []
            for state in grouped[rid]:
                eid = state.entity_id
                domain = eid.split(".")[0]
                safe = domain in {"light", "fan"} or (
                    domain == "switch"
                    and gang_types.get(eid) in {"light", "fan", "switch", "outlet"}
                )
                if not safe or state.state in {"unknown", "unavailable"}:
                    continue
                if configured and (
                    eid not in policy.get("eligible_entity_ids", [])
                    or not is_room_activity_candidate(state)
                ):
                    continue
                eligible.append(eid)
            count = sum(states[eid].state == "on" for eid in eligible)
            if count or configured:
                ranked.append(
                    {
                        **room,
                        "active_count": count,
                        "eligible_entity_ids": eligible,
                        "restore_pending_count": 0,
                        "most_recent_activity_at": None,
                    }
                )
        ranked.sort(key=lambda r: (-r["active_count"], r["room_id"]))
        return ranked, grouped, states

    async def context(self, scope=None):
        """Plan this window's room scenes for the scope.

        The two busiest rooms are fixed for the two-hour window (persisted,
        so a restart keeps them); the first gets an off and an eco plan,
        the second an off plan. Returns the same tuple shape as the rule
        runtime, with the plans standing in for rules and scenes.
        """
        ranked, grouped, states = await self.room_context(scope)
        now = self.clock()
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
        plans = []
        for index, rid in enumerate(selected):
            if rid not in rooms:
                continue
            for kind in ["room_off", "room_eco"] if index == 0 else ["room_off"]:
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
        data = await self.hass.async_add_executor_job(self.store.snapshot)
        data = {**data, "rules": [], "_generated_plans": plans}
        scenes = {
            p["scene_id"]: {
                **p["scene"],
                "entities": p["actions"],
                "works_during_energy_saving": True,
                "generated_room_v1": True,
                "room_id": p["room_id"],
                "kind": p["kind"],
            }
            for p in plans
        }
        return data, scenes, states, now, ZoneInfo(self.hass.config.time_zone)

    def candidates(self, context, scope, *, policy_checks=True):
        """Yield each plan whose every device the caller can see."""
        visible = self.visible(scope)
        for plan in context[0]["_generated_plans"]:
            if all(visible(a["entity_id"]) for a in plan["actions"]):
                yield {}, dict(plan), "eligible"

    def payload_from(self, context, member, scope):
        """Every plan this member has not suppressed and nobody tried to run."""
        data, _, _, now, _ = context
        plans = []
        for _, plan, _ in self.candidates(context, scope):
            slot = plan["suppression_id"]
            suppression = data["suppressions"].get(
                self.store.suppression_key(member, slot)
            )
            if suppression and datetime.fromisoformat(suppression["until"]) > now:
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
        return {
            "version": 1,
            "source": "generated_room_v1",
            "status": "available" if plans else "no_match",
            "suggestion": plans[0] if plans else None,
            "suggestions": plans,
            "refresh_at": window(now)[1].isoformat(),
        }

    async def payload(self, member, scope):
        """payload_from for the caller's scope; "unavailable" on failure."""
        try:
            return self.payload_from(await self.context(scope), member, scope)
        except (StorageError, sqlite3.Error, SuggestionError, ZoneInfoNotFoundError):
            return {
                "version": 1,
                "status": "unavailable",
                "suggestion": None,
                "suggestions": [],
            }
