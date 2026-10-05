"""HA boundary for suggestions: scoped reads and coalesced invalidation only."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
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
from .storage import StorageError
from .suggestions import SuggestionError, evaluate, next_boundary, state_value

_LOGGER = logging.getLogger(__name__)


class SuggestionRuntime:
    def __init__(self, hass, store, registry, *, clock=None, sunset=None):
        self.hass, self.store, self.registry = hass, store, registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sunset = sunset or self._sunset
        self._unsubs = []
        self._state_unsub = None
        self._ids = set()
        self._timer = self._debounce = self._task = None
        self._stopped = False
        self._fingerprint = None
        self._lock = asyncio.Lock()

    def _sunset(self, day):
        sun = self.hass.states.get("sun.sun")
        if state_value(sun) not in {"above_horizon", "below_horizon"}:
            return None
        return get_astral_event_date(self.hass, "sunset", day)

    async def context(self):
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
        return lambda eid: is_served(self.hass, eid) and in_scope(self.hass, eid, scope)

    def candidates(self, context, scope, *, policy_checks=True):
        data, scenes, states, now, zone = context
        for rule in sorted(data["rules"], key=lambda r: (-r["priority"], r["rule_id"])):
            suggestion, reason = evaluate(
                rule,
                scenes.get(rule["scene_id"]),
                states,
                now,
                zone,
                self.sunset,
                self.visible(scope),
                policy_checks=policy_checks,
            )
            yield rule, suggestion, reason

    def payload_from(self, context, member, scope):
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
        try:
            return self.payload_from(await self.context(), member, scope)
        except (StorageError, sqlite3.Error, SuggestionError, ZoneInfoNotFoundError):
            _LOGGER.warning("Suggestion snapshot unavailable")
            return {"version": 1, "status": "unavailable", "suggestion": None}

    async def start(self):
        for event in (
            EVENT_REGISTRY_CHANGED,
            EVENT_ENERGY_CHANGED,
            "core_config_updated",
        ):
            self._unsubs.append(self.hass.bus.async_listen(event, self._changed))
        await self.refresh()

    @callback
    def _changed(self, _event=None):
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
                # Only an invalidation signal is broadcast. No rule, room,
                # entity, scene, user, reason or occurrence appears in the frame.
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
                # Wake exactly at snooze expiry, even if the home is quiet.
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
