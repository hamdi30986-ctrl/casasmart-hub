"""Records room activity events from Home Assistant's state changes.

ActivityRecorder listens to every state change of a device the hub manages
(an imported control, served to the apps, in a registry room) and logs the
confirmed transitions the rules in room_intel.activity name, with their source.
The hub's own service calls get a context from command_context, so their
changes are attributed to the member, scene or Energy Saving behind them.

Rows are buffered on the event loop and written in one transaction every few
seconds, as Home Assistant's recorder commits. Nothing is logged until Home
Assistant has started and settled, so restored states don't become events.
The room ranking reads the latest events back with async_recent_events,
buffered ones included.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import Context, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started

from ..const import EVENT_REGISTRY_CHANGED
from ..filtering import area_id_of, is_served
from ..storage import StorageError
from .activity import (
    HUB_SOURCES,
    MAX_ROWS,
    ChatterGuard,
    RecentIds,
    attribute,
    prune,
    transition,
)
from .off_rooms import DARK_ACTIONS
from .ranking import imported_controls

if TYPE_CHECKING:
    from ..registry import RegistryEngine
    from ..storage import RoomActivityTable

_LOGGER = logging.getLogger(__name__)

# Home Assistant's recorder commits every 5 s by default; a burst (a scene, a
# room off) is written at once.
_FLUSH_SECONDS = 5.0
_FLUSH_ROWS = 100
# After Home Assistant reports started, integrations still replace restored
# states with real ones for a while.
_STARTUP_SETTLE_SECONDS = 120.0
_REGISTRY_DEBOUNCE_SECONDS = 1.0
_CHATTER_WARN_SECONDS = 3600.0

# A hub call's change can land a while after the call starts (a long scene),
# and an automation can act hours after it fired (a delay). The limits only
# bound memory: context ids are unique.
_HUB_CONTEXT_TTL = 900.0
_HUB_CONTEXT_MAX = 2048
_AUTOMATION_CONTEXT_TTL = 86400.0
_AUTOMATION_CONTEXT_MAX = 4096
_SERVICE_CONTEXT_TTL = 900.0
_SERVICE_CONTEXT_MAX = 2048

# Fired with the context an automation or script runs in
# (homeassistant.components.automation and .script).
_EVENT_AUTOMATION_TRIGGERED = "automation_triggered"
_EVENT_SCRIPT_STARTED = "script_started"


class ActivityRecorder:
    """Logs the managed devices' transitions to the room activity table."""

    def __init__(
        self,
        hass: HomeAssistant,
        registry: RegistryEngine,
        table: RoomActivityTable,
        *,
        startup_settle: float = _STARTUP_SETTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._hass = hass
        self._registry = registry
        self._table = table
        self._startup_settle = startup_settle
        self._clock = clock
        self._wall_clock = wall_clock

        self._managed: frozenset[str] = frozenset()
        self._rooms: frozenset[str] = frozenset()
        self._buffer: list[dict[str, Any]] = []
        self._flush_lock = asyncio.Lock()
        self._cancel_flush: Callable[[], None] | None = None
        self._registry_debounce: asyncio.TimerHandle | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._ready = False
        self._stopped = False
        self._read_failed = False
        # When Home Assistant reported started (wall clock), None when this
        # recorder started on a running Home Assistant (a reload).
        self.started_at: float | None = None

        self._hub_contexts = RecentIds(_HUB_CONTEXT_TTL, _HUB_CONTEXT_MAX)
        self._automation_contexts = RecentIds(
            _AUTOMATION_CONTEXT_TTL, _AUTOMATION_CONTEXT_MAX
        )
        self._service_contexts = RecentIds(_SERVICE_CONTEXT_TTL, _SERVICE_CONTEXT_MAX)
        self._chatter = ChatterGuard()
        self._chatter_warned: dict[str, float] = {}

    # -- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Load the managed devices, listen, and prune once.

        The owner runs async_prune on a timer after that.
        """
        await self._async_load_managed()
        bus = self._hass.bus
        self._unsubs.extend(
            (
                bus.async_listen(
                    EVENT_STATE_CHANGED,
                    self._on_state_changed,
                    event_filter=self._is_managed,
                ),
                bus.async_listen(_EVENT_AUTOMATION_TRIGGERED, self._on_run_started),
                bus.async_listen(_EVENT_SCRIPT_STARTED, self._on_run_started),
                bus.async_listen(EVENT_CALL_SERVICE, self._on_service_call),
                bus.async_listen(EVENT_REGISTRY_CHANGED, self._on_registry_changed),
            )
        )
        if self._hass.state is CoreState.running:
            # A reload: states are live, nothing is being restored.
            self._ready = True
        else:
            self._unsubs.append(async_at_started(self._hass, self._on_ha_started))
        await self.async_prune()

    async def async_stop(self) -> None:
        """Stop listening and write what is buffered. Safe to call twice."""
        self._stopped = True
        while self._unsubs:
            self._unsubs.pop()()
        if self._registry_debounce is not None:
            self._registry_debounce.cancel()
            self._registry_debounce = None
        await self.async_flush()

    @callback
    def _on_ha_started(self, _hass: HomeAssistant) -> None:
        self.started_at = self._wall_clock()
        self._unsubs.append(
            async_call_later(self._hass, self._startup_settle, self._on_settled)
        )

    @callback
    def _on_settled(self, _now: datetime) -> None:
        self._ready = True

    # -- the hub's own calls -----------------------------------------------

    @callback
    def command_context(self, source: str, member_id: str | None = None) -> Context:
        """A new context for a hub service call, remembered as source/member."""
        if source not in HUB_SOURCES:
            raise ValueError(f"Not a hub command source: {source!r}")
        context = Context()
        self._hub_contexts.add(context.id, (source, member_id), self._clock())
        return context

    # -- listeners ---------------------------------------------------------

    @callback
    def _is_managed(self, event_data: dict[str, Any]) -> bool:
        """Cheap first check, run for every state change in Home Assistant."""
        return event_data.get("entity_id") in self._managed

    @callback
    def _on_state_changed(self, event: Event) -> None:
        if not self._ready or self._stopped:
            return
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if old_state is None or new_state is None:
            return
        entity_id = event.data["entity_id"]
        action = transition(entity_id, old_state.state, new_state.state)
        if action is None:
            return
        room_id = area_id_of(self._hass, entity_id, self._registry)
        if room_id not in self._rooms or not is_served(self._hass, entity_id):
            return
        now = self._clock()
        if not self._chatter.allow(entity_id, now):
            self._warn_chatter(entity_id, now)
            return
        context = event.context
        source, member_id = attribute(
            hub_origin=self._hub_contexts.get(context.id, now),
            user_id=context.user_id,
            parent_id=context.parent_id,
            automation_context=(
                self._automation_contexts.get(context.id, now) is not None
                or self._automation_contexts.get(context.parent_id, now) is not None
            ),
            service_call_context=self._service_contexts.get(context.id, now)
            is not None,
        )
        t = event.time_fired_timestamp
        self._buffer.append(
            {
                "t": int(t),
                "utc_offset_min": self._utc_offset_min(t),
                "room_id": room_id,
                "entity_id": entity_id,
                "action": action,
                "source": source,
                "member_id": member_id,
            }
        )
        if len(self._buffer) >= _FLUSH_ROWS:
            self._schedule_flush(0)
        elif self._cancel_flush is None:
            self._schedule_flush(_FLUSH_SECONDS)

    @callback
    def _on_run_started(self, event: Event) -> None:
        """Remember the context an automation or script runs in."""
        self._automation_contexts.add(event.context.id, True, self._clock())

    @callback
    def _on_service_call(self, event: Event) -> None:
        """Remember a service call's context when nothing else names its caller."""
        context = event.context
        now = self._clock()
        if (
            context.user_id
            or context.parent_id
            or self._hub_contexts.get(context.id, now) is not None
        ):
            return
        self._service_contexts.add(context.id, True, now)

    @callback
    def _on_registry_changed(self, _event: Event) -> None:
        """Reload the managed devices and rooms, once a burst of edits is over."""
        if self._stopped:
            return
        if self._registry_debounce is not None:
            self._registry_debounce.cancel()
        self._registry_debounce = self._hass.loop.call_later(
            _REGISTRY_DEBOUNCE_SECONDS, self._kick_reload
        )

    @callback
    def _kick_reload(self) -> None:
        self._registry_debounce = None
        if not self._stopped:
            self._hass.async_create_task(self._async_load_managed())

    # -- writing -----------------------------------------------------------

    @callback
    def _schedule_flush(self, delay: float) -> None:
        if self._cancel_flush is not None:
            self._cancel_flush()
        self._cancel_flush = async_call_later(self._hass, delay, self._on_flush_due)

    @callback
    def _on_flush_due(self, _now: datetime) -> None:
        self._cancel_flush = None
        self._hass.async_create_background_task(
            self.async_flush(), "casasmart room activity flush"
        )

    async def async_flush(self) -> None:
        """Write the buffered events now, in one transaction."""
        async with self._flush_lock:
            if self._cancel_flush is not None:
                self._cancel_flush()
                self._cancel_flush = None
            rows, self._buffer = self._buffer, []
            if not rows:
                return
            try:
                await self._hass.async_add_executor_job(self._table.append_many, rows)
            except (StorageError, sqlite3.Error, ValueError) as err:
                _LOGGER.warning(
                    "Room activity: %d events not saved: %s", len(rows), err
                )

    async def async_recent_events(
        self, since_t: int, room_ids: list[str]
    ) -> list[dict[str, Any]]:
        """The rooms' on/off/set events since since_t, newest first.

        Includes the events not written yet. Read under the flush lock, so
        each event is seen once: written, or still buffered. A failed read
        returns none (the ranking then has no just_off), warned once.
        """
        if not room_ids:
            return []
        rooms = set(room_ids)
        async with self._flush_lock:
            try:
                rows = await self._hass.async_add_executor_job(
                    partial(
                        self._table.recent_in_rooms,
                        room_ids,
                        since_t=since_t,
                        actions=DARK_ACTIONS,
                    )
                )
            except (StorageError, sqlite3.Error, ValueError) as err:
                log = _LOGGER.debug if self._read_failed else _LOGGER.warning
                log("Room activity could not be read: %s", err)
                self._read_failed = True
                return []
            pending = [
                row
                for row in self._buffer
                if row["room_id"] in rooms
                and row["t"] >= since_t
                and row["action"] in DARK_ACTIONS
            ]
        self._read_failed = False
        return [*reversed(pending), *rows]

    async def async_prune(self, _now: datetime | None = None) -> None:
        """Apply the retention rules (room_intel.activity.prune)."""
        try:
            removed = await self._hass.async_add_executor_job(
                prune, self._table, int(self._wall_clock())
            )
        except (StorageError, sqlite3.Error) as err:
            _LOGGER.warning("Room activity prune failed: %s", err)
            return
        if removed["cap"]:
            _LOGGER.warning(
                "Room activity log reached %d rows; dropped the oldest %d",
                MAX_ROWS,
                removed["cap"],
            )
        _LOGGER.debug("Room activity pruned: %s", removed)

    async def async_forget_member(self, member_id: str) -> None:
        """Delete a member's events, including any not written yet."""
        async with self._flush_lock:
            self._buffer = [
                row for row in self._buffer if row["member_id"] != member_id
            ]
            self._hub_contexts.discard_values(lambda origin: origin[1] == member_id)
            await self._hass.async_add_executor_job(
                self._table.delete_member, member_id
            )

    async def async_clear(self) -> None:
        """Delete every event, including any not written yet (factory reset)."""
        async with self._flush_lock:
            self._buffer.clear()
            await self._hass.async_add_executor_job(self._table.clear)

    # -- helpers -----------------------------------------------------------

    async def _async_load_managed(self) -> None:
        """Read which entities and rooms the hub manages (one executor job)."""

        def read() -> tuple[dict[str, str | None], list[dict[str, Any]]]:
            return (
                imported_controls(self._registry.list_user_devices()),
                self._registry.list_rooms(),
            )

        try:
            controls, rooms = await self._hass.async_add_executor_job(read)
        except (StorageError, sqlite3.Error) as err:
            _LOGGER.warning("Room activity could not read the registry: %s", err)
            return
        self._managed = frozenset(controls)
        self._rooms = frozenset(room["room_id"] for room in rooms)
        self._chatter.forget_except(self._managed)

    def _utc_offset_min(self, t: float) -> int:
        """The hub time zone's UTC offset at t, in minutes (0 if unknown)."""
        try:
            zone = ZoneInfo(self._hass.config.time_zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return 0
        offset = datetime.fromtimestamp(t, zone).utcoffset()
        return int(offset.total_seconds() // 60) if offset is not None else 0

    def _warn_chatter(self, entity_id: str, now: float) -> None:
        last = self._chatter_warned.get(entity_id)
        if last is not None and now - last < _CHATTER_WARN_SECONDS:
            return
        self._chatter_warned[entity_id] = now
        _LOGGER.warning(
            "Room activity: %s changes too often; its extra changes are not "
            "logged (a faulty device or an automation loop?)",
            entity_id,
        )
