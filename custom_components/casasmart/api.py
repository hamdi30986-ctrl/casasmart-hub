"""CasaSmart REST API (Track B — B1.3 skeleton).

Two unauthenticated endpoints:

- ``GET /api/casasmart/handshake`` — the version contract between app and
  hub. The app sends ``X-CasaSmart-API-Version`` and decides from the
  response whether it can talk to this hub (plan: "API Version Handshake").
  No auth: the app must read this BEFORE it has any credentials, and the
  response contains nothing sensitive (versions only).
- ``GET /api/casasmart/health`` — liveness probe for external monitoring
  (watchdog, restore LXC, uptime checks). Reports integration + storage
  state. No auth for the same reason a load-balancer health check has none.

Everything else added in B1.4+ requires auth — these two are the only
deliberate exceptions, and both are read-only.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    API_VERSION,
    API_VERSION_HEADER,
    DOMAIN,
    MIN_APP_VERSION,
    SUPPORTED_API_VERSIONS,
)

if TYPE_CHECKING:
    from . import CasaSmartRuntimeData

_LOGGER = logging.getLogger(__name__)


def async_register_views(hass: HomeAssistant, hub_version: str) -> None:
    """Register the CasaSmart REST views (idempotent across entry reloads).

    HA's router can't unregister views, so a config-entry reload would
    register duplicates — guard with a domain-scoped flag.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("views_registered"):
        return
    hass.http.register_view(CasaSmartHandshakeView(hub_version))
    hass.http.register_view(CasaSmartHealthView(hass, hub_version))
    domain_data["views_registered"] = True
    _LOGGER.debug("CasaSmart REST views registered")


def _get_runtime_data(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    """Return the loaded entry's runtime data, or None if not loaded."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    return entries[0].runtime_data


class CasaSmartHandshakeView(HomeAssistantView):
    """GET /api/casasmart/handshake — the app↔hub version contract."""

    url = f"/api/{DOMAIN}/handshake"
    name = f"api:{DOMAIN}:handshake"
    requires_auth = False

    def __init__(self, hub_version: str) -> None:
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.Response:
        """Return the version contract; echo compatibility if app sent its version."""
        body: dict[str, Any] = {
            "api_version": API_VERSION,
            "min_app_version": MIN_APP_VERSION,
            "hub_version": self._hub_version,
            "supported_api_versions": list(SUPPORTED_API_VERSIONS),
        }

        # Additive convenience: if the app declared its API version, tell it
        # outright whether we speak it. Malformed header -> 400, not a guess.
        app_api_version = request.headers.get(API_VERSION_HEADER)
        if app_api_version is not None:
            try:
                requested = int(app_api_version)
            except ValueError:
                return self.json_message(
                    f"Invalid {API_VERSION_HEADER} header: {app_api_version!r}",
                    HTTPStatus.BAD_REQUEST,
                )
            body["compatible"] = requested in SUPPORTED_API_VERSIONS

        return self.json(body)


class CasaSmartHealthView(HomeAssistantView):
    """GET /api/casasmart/health — liveness probe for external monitoring."""

    url = f"/api/{DOMAIN}/health"
    name = f"api:{DOMAIN}:health"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, hub_version: str) -> None:
        self._hass = hass
        self._hub_version = hub_version

    async def get(self, request: web.Request) -> web.Response:
        """Report integration + storage health.

        200 with status "ok" only when the entry is loaded AND the storage
        layer answers a real read (schema_version hits SQLite). Anything
        else is 503 so dumb HTTP monitors can alert on status code alone.
        """
        body: dict[str, Any] = {
            "status": "ok",
            "hub_version": self._hub_version,
            "api_version": API_VERSION,
        }

        runtime_data = _get_runtime_data(self._hass)
        if runtime_data is None:
            body["status"] = "error"
            body["storage"] = "unavailable"
            return self.json(body, HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            # schema_version is a property that executes a real PRAGMA read —
            # proves the DB is open and answering, not just that the object
            # exists. Wrapped in a lambda so the read runs in the executor.
            schema_version = await self._hass.async_add_executor_job(
                lambda: runtime_data.storage.schema_version
            )
        except Exception:  # noqa: BLE001 — health must never 500 with a traceback
            _LOGGER.exception("Health check: storage read failed")
            body["status"] = "error"
            body["storage"] = "error"
            return self.json(body, HTTPStatus.SERVICE_UNAVAILABLE)

        body["storage"] = "ok"
        body["schema_version"] = schema_version
        return self.json(body)
