"""Alarm REST endpoints over AlarmEngine.

Endpoints under /api/casasmart/alarm, with the permission each needs:

- GET    /state              arm-state snapshot (alarm.read)
- POST   /arm                arm away, home or night (alarm.arm)
- POST   /disarm             disarm or silence (alarm.arm)
- GET    /zones              sensor -> zone map (alarm.read)
- PUT    /zones/{entity_id}  assign a sensor's zone (alarm.manage)
- DELETE /zones/{entity_id}  unassign a sensor (alarm.manage)
- GET    /settings           default entry/exit delays (alarm.read)
- PUT    /settings           change the default delays (alarm.manage)
- GET    /history            event history (alarm.read)

Admins and sub-admins hold these permissions; plain users have no alarm
access. Every change fires EVENT_ALARM_CHANGED, which also makes the alarm
adapter re-sync its entry-delay timer, so an app disarm cancels a countdown.
"""

from __future__ import annotations

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


def get_alarm(hass: HomeAssistant) -> AlarmEngine | None:
    """The loaded entry's alarm engine, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.alarm


def _serialize_zones(zones: dict[str, dict]) -> list[dict]:
    """{entity_id: {zone, name}} -> a list sorted by entity id."""
    return [
        {"entity_id": entity_id, "zone": record["zone"], "name": record["name"]}
        for entity_id, record in sorted(zones.items())
    ]


# -- executor jobs (storage-touching engine calls) -----------------------------
# async_add_executor_job passes positional arguments only; these map them to
# the engine's keyword arguments.


def _arm_job(alarm, mode, actor, exit_delay, entry_delay):
    return alarm.arm(mode, actor=actor, exit_delay=exit_delay, entry_delay=entry_delay)


def _disarm_job(alarm, actor):
    return alarm.disarm(actor=actor)


def _set_zone_job(alarm, entity_id, zone, name):
    return alarm.set_zone(entity_id, zone, name)


def _set_settings_job(alarm, entry_delay, exit_delay):
    return alarm.set_settings(entry_delay=entry_delay, exit_delay=exit_delay)


# -- views --------------------------------------------------------------------


class _AlarmView(HomeAssistantView):
    """Shared plumbing for the alarm views."""

    requires_auth = False  # CasaSmart JWT gate in-handler

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _alarm_or_503(self) -> tuple[AlarmEngine | None, web.Response | None]:
        """(engine, None), or (None, 503) while the hub is loading."""
        alarm = get_alarm(self._hass)
        if alarm is None:
            return None, self.json_message(
                "Hub not ready", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return alarm, None

    def _notify_change(self) -> None:
        """Tell connected apps and the alarm adapter the state changed."""
        self._hass.bus.async_fire(EVENT_ALARM_CHANGED, {})


class CasaSmartAlarmStateView(_AlarmView):
    """GET /api/casasmart/alarm/state: the arm-state snapshot."""

    url = f"/api/{DOMAIN}/alarm/state"
    name = f"api:{DOMAIN}:alarm:state"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        # In memory, so no executor hop.
        return self.json({"state": alarm.snapshot()})


class CasaSmartAlarmArmView(_AlarmView):
    """POST /api/casasmart/alarm/arm: arm away, home or night.

    Body: {"mode": "armed_away" | "armed_home" | "armed_night"}, plus optional
    exit_delay and entry_delay (whole seconds, 0-600; the stored defaults
    apply otherwise). The caller's device id is recorded in the history.
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
    """POST /api/casasmart/alarm/disarm: disarm from any state."""

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
    """GET /api/casasmart/alarm/zones: the sensor -> zone assignments."""

    url = f"/api/{DOMAIN}/alarm/zones"
    name = f"api:{DOMAIN}:alarm:zones"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready
        # In memory, so no executor hop.
        return self.json({"zones": _serialize_zones(alarm.zones())})


class CasaSmartAlarmZoneView(_AlarmView):
    """PUT/DELETE /api/casasmart/alarm/zones/{entity_id}: one assignment."""

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
            await self._hass.async_add_executor_job(alarm.remove_zone, entity_id)
        except UnknownZoneError:
            return self.json_message(
                f"No sensor assigned under {entity_id!r}", HTTPStatus.NOT_FOUND
            )
        self._notify_change()
        return self.json({"deleted": entity_id})


class CasaSmartAlarmHistoryView(_AlarmView):
    """GET /api/casasmart/alarm/history?limit=N: event history, newest first."""

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
            history = await self._hass.async_add_executor_job(alarm.history, limit)
        except AlarmError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json({"history": history})


class CasaSmartAlarmSettingsView(_AlarmView):
    """GET/PUT /api/casasmart/alarm/settings: the default entry/exit delays.

    alarm.read to view, alarm.manage to change. PUT takes entry_delay and/or
    exit_delay in seconds (0-600); omitted fields keep their value.
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
        # In memory, so no executor hop.
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
