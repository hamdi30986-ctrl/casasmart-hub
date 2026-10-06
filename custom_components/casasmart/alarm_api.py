"""CasaSmart alarm REST endpoints (Phase 6, block B13).

The app's thin-client surface over the hub-side ``AlarmEngine``. Matches the
established API pattern (``tank_api`` / ``registry_api``): plain views on HA's
port and the B10 TLS port both serve these, every handler gates in-band with
``authenticate_request``, and storage-touching engine calls hop the executor.

Endpoints (one panel with zone attributes — plan B13):

- ``GET    /api/casasmart/alarm/state``            — arm-state snapshot
- ``POST   /api/casasmart/alarm/arm``              — arm away/home/night
- ``POST   /api/casasmart/alarm/disarm``           — disarm / silence
- ``GET    /api/casasmart/alarm/zones``            — sensor -> zone map
- ``PUT    /api/casasmart/alarm/zones/{entity_id}``— assign a sensor's zone
- ``DELETE /api/casasmart/alarm/zones/{entity_id}``— unassign a sensor
- ``GET    /api/casasmart/alarm/settings``         — default entry/exit delays
- ``PUT    /api/casasmart/alarm/settings``         — edit default delays
- ``GET    /api/casasmart/alarm/history``          — event history

Role gates (plan roles table — a plain user has NO alarm access):
``alarm.read`` for state/zones/history, ``alarm.arm`` for arm/disarm,
``alarm.manage`` for zone configuration. Every mutation fires
``EVENT_ALARM_CHANGED`` so the WS server nudges connected apps and the alarm
adapter re-syncs its entry-delay timer (an app-driven disarm cancelling a
running countdown rides this exact path).
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .alarm import AlarmEngine, AlarmError, UnknownZoneError
from .auth_api import authenticate_request, json_body
from .const import DOMAIN, EVENT_ALARM_CHANGED

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def get_alarm(hass: HomeAssistant) -> AlarmEngine | None:
    """The loaded entry's alarm engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.alarm


def _serialize_zones(zones: dict[str, dict]) -> list[dict]:
    """``{entity_id: {zone, name}}`` -> a stable, app-friendly list."""
    return [
        {"entity_id": entity_id, "zone": record["zone"], "name": record["name"]}
        for entity_id, record in sorted(zones.items())
    ]


class _AlarmView(HomeAssistantView):
    """Shared plumbing for the alarm views."""

    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _alarm_or_503(self) -> tuple[AlarmEngine | None, web.Response | None]:
        alarm = get_alarm(self._hass)
        if alarm is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return alarm, None

    def _notify_change(self) -> None:
        """Tell connected apps + the adapter the arm state moved."""
        self._hass.bus.async_fire(EVENT_ALARM_CHANGED, {})


class CasaSmartAlarmStateView(_AlarmView):
    """GET /api/casasmart/alarm/state — the panel snapshot."""

    url = f"/api/{DOMAIN}/alarm/state"
    name = f"api:{DOMAIN}:alarm:state"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        # snapshot() is pure CPU (in-memory mirror) — no executor hop.
        return self.json({"state": alarm.snapshot()})


class CasaSmartAlarmArmView(_AlarmView):
    """POST /api/casasmart/alarm/arm — command an armed mode.

    Body: ``{"mode": "armed_away|armed_home|armed_night", "exit_delay"?,
    "entry_delay"?}``. Delays are optional and clamped by the engine.
    """

    url = f"/api/{DOMAIN}/alarm/arm"
    name = f"api:{DOMAIN}:alarm:arm"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "alarm.arm")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            snapshot = await self._hass.async_add_executor_job(
                _arm_job,
                alarm,
                payload.get("mode"),
                claims.get("sub"),
                payload.get("exit_delay"),
                payload.get("entry_delay"),
            )
        except AlarmError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._notify_change()
        return self.json({"state": snapshot})


class CasaSmartAlarmDisarmView(_AlarmView):
    """POST /api/casasmart/alarm/disarm — disarm from any state."""

    url = f"/api/{DOMAIN}/alarm/disarm"
    name = f"api:{DOMAIN}:alarm:disarm"

    async def post(self, request: web.Request) -> web.Response:
        claims, error = authenticate_request(self._hass, request, "alarm.arm")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        snapshot = await self._hass.async_add_executor_job(
            _disarm_job, alarm, claims.get("sub")
        )
        self._notify_change()
        return self.json({"state": snapshot})


class CasaSmartAlarmZonesView(_AlarmView):
    """GET /api/casasmart/alarm/zones — the sensor -> zone assignments."""

    url = f"/api/{DOMAIN}/alarm/zones"
    name = f"api:{DOMAIN}:alarm:zones"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        # zones() copies the in-memory mirror — pure CPU, no executor hop.
        return self.json({"zones": _serialize_zones(alarm.zones())})


class CasaSmartAlarmZoneView(_AlarmView):
    """PUT/DELETE /api/casasmart/alarm/zones/{entity_id} — one assignment."""

    url = f"/api/{DOMAIN}/alarm/zones/{{entity_id}}"
    name = f"api:{DOMAIN}:alarm:zone"

    async def put(self, request: web.Request, entity_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.manage")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            record = await self._hass.async_add_executor_job(
                _set_zone_job,
                alarm,
                entity_id,
                payload.get("zone"),
                payload.get("name"),
            )
        except AlarmError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._notify_change()
        return self.json({"entity_id": entity_id, **record})

    async def delete(self, request: web.Request, entity_id: str) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.manage")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        try:
            await self._hass.async_add_executor_job(
                alarm.remove_zone, entity_id
            )
        except UnknownZoneError:
            return self.json_message(
                f"No sensor assigned under {entity_id!r}", HTTPStatus.NOT_FOUND
            )
        self._notify_change()
        return self.json({"deleted": entity_id})


class CasaSmartAlarmHistoryView(_AlarmView):
    """GET /api/casasmart/alarm/history?limit=N — event history, newest first."""

    url = f"/api/{DOMAIN}/alarm/history"
    name = f"api:{DOMAIN}:alarm:history"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        raw_limit = request.query.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError:
            return self.json_message(
                f"Invalid limit: {raw_limit!r}", HTTPStatus.BAD_REQUEST
            )
        try:
            history = await self._hass.async_add_executor_job(
                alarm.history, limit
            )
        except AlarmError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json({"history": history})


class CasaSmartAlarmSettingsView(_AlarmView):
    """GET/PUT /api/casasmart/alarm/settings — hub-owned default delays.

    The app's settings screen reads these on open and writes edits here, so
    they live on the hub (phone-independent) like everything else. ``read``
    to view, ``manage`` to change.

    PUT body: ``{"entry_delay"?: int, "exit_delay"?: int}`` (seconds). Omitted
    fields keep their current value; both are clamped by the engine.
    """

    url = f"/api/{DOMAIN}/alarm/settings"
    name = f"api:{DOMAIN}:alarm:settings"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        # get_settings() copies the in-memory mirror — pure CPU, no executor hop.
        return self.json({"settings": alarm.get_settings()})

    async def put(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.manage")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        payload = await json_body(request)
        if payload is None:
            return self.json_message(
                "Body must be a JSON object", HTTPStatus.BAD_REQUEST
            )
        try:
            settings = await self._hass.async_add_executor_job(
                _set_settings_job,
                alarm,
                payload.get("entry_delay"),
                payload.get("exit_delay"),
            )
        except AlarmError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        self._notify_change()
        return self.json({"settings": settings})


# -- executor jobs (storage-touching engine calls) -----------------------------
# Small module-level helpers so async_add_executor_job gets a plain callable
# instead of a closure capturing request state.


def _arm_job(alarm, mode, actor, exit_delay, entry_delay):
    return alarm.arm(
        mode, actor=actor, exit_delay=exit_delay, entry_delay=entry_delay
    )


def _disarm_job(alarm, actor):
    return alarm.disarm(actor=actor)


def _set_zone_job(alarm, entity_id, zone, name):
    return alarm.set_zone(entity_id, zone, name)


def _set_settings_job(alarm, entry_delay, exit_delay):
    return alarm.set_settings(entry_delay=entry_delay, exit_delay=exit_delay)
