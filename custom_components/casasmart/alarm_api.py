"""CasaSmart runtime component."""

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
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    runtime_data: CasaSmartRuntimeData = entries[0].runtime_data
    return runtime_data.alarm


def _serialize_zones(zones: dict[str, dict]) -> list[dict]:
    """CasaSmart runtime component."""
    return [
        {"entity_id": entity_id, "zone": record["zone"], "name": record["name"]}
        for entity_id, record in sorted(zones.items())
    ]


class _AlarmView(HomeAssistantView):
    """CasaSmart runtime component."""

    requires_auth = False

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
        """CasaSmart runtime component."""
        self._hass.bus.async_fire(EVENT_ALARM_CHANGED, {})


class CasaSmartAlarmStateView(_AlarmView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/alarm/state"
    name = f"api:{DOMAIN}:alarm:state"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready

        return self.json({"state": alarm.snapshot()})


class CasaSmartAlarmArmView(_AlarmView):
    """CasaSmart runtime component."""

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
    """CasaSmart runtime component."""

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/alarm/zones"
    name = f"api:{DOMAIN}:alarm:zones"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready

        return self.json({"zones": _serialize_zones(alarm.zones())})


class CasaSmartAlarmZoneView(_AlarmView):
    """CasaSmart runtime component."""

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
    """CasaSmart runtime component."""

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
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/alarm/settings"
    name = f"api:{DOMAIN}:alarm:settings"

    async def get(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "alarm.read")
        if error is not None:
            return error
        alarm, not_ready = self._alarm_or_503()
        if not_ready is not None:
            return not_ready

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
