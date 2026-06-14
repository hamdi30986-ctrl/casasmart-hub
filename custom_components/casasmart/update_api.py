"""Self-update endpoints (Track B — B5).

Piece 1 of B5: the read-only status surface the app polls on launch to
raise a red dot and show a changelog.

- ``GET /api/casasmart/update/status`` — ``{current_version,
  latest_version, update_available, changelog, published_at,
  release_url, checked_at}``. Gated ``update.read`` (every role sees
  it). ``current_version`` is the running integration version;
  ``latest_version`` comes from the repo's latest GitHub release,
  fetched at most once per ``UPDATE_CHECK_TTL_SECONDS`` and cached.

The actual self-replace + restart (``POST /update/install``, admin-only)
is Piece 3 and is NOT in this module yet — the ``update.install``
permission already exists so the gate is ready when it lands.

The version math and release parsing are pure and live in ``update.py``;
this module owns the aiohttp fetch, the TTL cache, and the HA view.
"""

from __future__ import annotations

import asyncio
import logging
import time
from http import HTTPStatus
from typing import Any

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .auth_api import authenticate_request
from .const import (
    DOMAIN,
    UPDATE_CHECK_TTL_SECONDS,
    UPDATE_GITHUB_REPO,
    UPDATE_REPO_CONFIG_KEY,
)
from .update import ReleaseInfo, is_newer, parse_release

_LOGGER = logging.getLogger(__name__)

# GitHub's "newest non-prerelease, non-draft release" endpoint. A repo with
# zero published releases answers 404 — handled as "no update", never an error.
_GITHUB_LATEST_URL = "https://api.github.com/repos/{repo}/releases/latest"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "CasaSmart-Hub",
}
# The hub is a small box on a home LAN — don't let a slow/blocked GitHub hold
# an HTTP handler open. On timeout we serve the cache (or "unknown latest").
_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _resolve_repo(hass: HomeAssistant) -> str:
    """The GitHub repo to check — hub_config override, else the const default."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entries:
        runtime_data = entries[0].runtime_data
        override = getattr(runtime_data, "hub_config", {}).get(UPDATE_REPO_CONFIG_KEY)
        if isinstance(override, str) and override.strip():
            return override.strip()
    return UPDATE_GITHUB_REPO


class UpdateChecker:
    """Caches the repo's latest release, refreshing lazily past the TTL.

    One instance per hub (shared across the plain + TLS listeners via
    ``get_or_create_checker``). ``async_status`` is the only entry point:
    it refreshes if the cache is stale, then returns the status dict.
    GitHub being unreachable is never fatal — the last known release (or
    "no latest yet") is served and the failure is logged once per window.
    """

    def __init__(self, hass: HomeAssistant, current_version: str) -> None:
        self._hass = hass
        self._current_version = current_version
        self._lock = asyncio.Lock()
        self._latest: ReleaseInfo | None = None
        self._checked_monotonic: float | None = None
        self._checked_at_iso: str | None = None

    def _is_fresh(self) -> bool:
        if self._checked_monotonic is None:
            return False
        return (time.monotonic() - self._checked_monotonic) < UPDATE_CHECK_TTL_SECONDS

    async def async_status(self) -> dict[str, Any]:
        """Return the status dict, refreshing the cache first if it's stale."""
        if not self._is_fresh():
            await self._async_refresh()
        return self._build_status()

    async def _async_refresh(self) -> None:
        """Fetch the latest release once; concurrent callers share the result."""
        async with self._lock:
            # A caller may have refreshed while we waited on the lock.
            if self._is_fresh():
                return
            repo = _resolve_repo(self._hass)
            url = _GITHUB_LATEST_URL.format(repo=repo)
            session = async_get_clientsession(self._hass)
            try:
                async with session.get(
                    url, headers=_GITHUB_HEADERS, timeout=_FETCH_TIMEOUT
                ) as response:
                    if response.status == HTTPStatus.NOT_FOUND:
                        # Repo has no published releases — a valid "no update"
                        # state, not an error. Cache it so we don't re-poll.
                        self._latest = None
                        self._mark_checked()
                        return
                    if response.status != HTTPStatus.OK:
                        _LOGGER.warning(
                            "Update check: GitHub returned %s for %s",
                            response.status,
                            repo,
                        )
                        return  # keep the previous cache; don't mark fresh
                    payload = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Update check: GitHub unreachable (%s): %s", repo, err)
                return  # keep the previous cache; retry on the next call

            self._latest = parse_release(payload)
            self._mark_checked()

    def _mark_checked(self) -> None:
        self._checked_monotonic = time.monotonic()
        self._checked_at_iso = dt_util.utcnow().isoformat()

    def _build_status(self) -> dict[str, Any]:
        latest = self._latest
        latest_version = latest.version if latest is not None else None
        return {
            "current_version": self._current_version,
            "latest_version": latest_version,
            "update_available": (
                latest_version is not None
                and is_newer(self._current_version, latest_version)
            ),
            "changelog": latest.changelog if latest is not None else None,
            "published_at": latest.published_at if latest is not None else None,
            "release_url": latest.release_url if latest is not None else None,
            "checked_at": self._checked_at_iso,
        }


def get_or_create_checker(hass: HomeAssistant, current_version: str) -> UpdateChecker:
    """The hub's single ``UpdateChecker``, created on first use.

    ``build_views`` runs once per listener (plain + TLS), so the checker
    is stashed in ``hass.data`` to share one cache across both surfaces.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    checker = domain_data.get("update_checker")
    if checker is None:
        checker = UpdateChecker(hass, current_version)
        domain_data["update_checker"] = checker
    return checker


class CasaSmartUpdateStatusView(HomeAssistantView):
    """GET /api/casasmart/update/status — current vs latest hub version."""

    url = f"/api/{DOMAIN}/update/status"
    name = f"api:{DOMAIN}:update:status"
    requires_auth = False  # CasaSmart JWT gate (B1.6)

    def __init__(self, hass: HomeAssistant, checker: UpdateChecker) -> None:
        self._hass = hass
        self._checker = checker

    async def get(self, request: web.Request) -> web.Response:
        """Report the update status. Never 500s on a GitHub hiccup — a
        failed check degrades to ``latest_version: null`` (no update)."""
        _, error = authenticate_request(self._hass, request, "update.read")
        if error is not None:
            return error
        return self.json(await self._checker.async_status())
