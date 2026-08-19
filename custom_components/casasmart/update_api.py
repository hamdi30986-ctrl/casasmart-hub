"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import logging
import re
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
    UPDATE_REPO_CONFIG_KEY,
)
from .update import ReleaseInfo, is_newer, parse_release

_LOGGER = logging.getLogger(__name__)



_GITHUB_LATEST_URL = "https://api.github.com/repos/{repo}/releases/latest"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "CasaSmart-Hub",
}


_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
_GITHUB_REPO_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"
)


def _resolve_repo(hass: HomeAssistant) -> str | None:
    """CasaSmart runtime component."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entries:
        runtime_data = entries[0].runtime_data
        override = getattr(runtime_data, "hub_config", {}).get(UPDATE_REPO_CONFIG_KEY)
        if isinstance(override, str):
            candidate = override.strip()
            if _GITHUB_REPO_RE.fullmatch(candidate):
                return candidate
    return None


class UpdateChecker:
    """CasaSmart runtime component."""

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
        """CasaSmart runtime component."""
        if not self._is_fresh():
            await self._async_refresh()
        return self._build_status()

    async def async_download_url(self) -> str | None:
        """CasaSmart runtime component."""
        if not self._is_fresh():
            await self._async_refresh()
        return self._latest.download_url if self._latest is not None else None

    async def _async_refresh(self) -> None:
        """CasaSmart runtime component."""
        async with self._lock:

            if self._is_fresh():
                return
            repo = _resolve_repo(self._hass)
            if repo is None:
                self._latest = None
                self._mark_checked()
                return
            url = _GITHUB_LATEST_URL.format(repo=repo)
            session = async_get_clientsession(self._hass)
            try:
                async with session.get(
                    url, headers=_GITHUB_HEADERS, timeout=_FETCH_TIMEOUT
                ) as response:
                    if response.status == HTTPStatus.NOT_FOUND:


                        self._latest = None
                        self._mark_checked()
                        return
                    if response.status != HTTPStatus.OK:
                        _LOGGER.warning(
                            "Update check: GitHub returned %s for %s",
                            response.status,
                            repo,
                        )
                        return
                    payload = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Update check: GitHub unreachable (%s): %s", repo, err)
                return

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
    """CasaSmart runtime component."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    checker = domain_data.get("update_checker")
    if checker is None:
        checker = UpdateChecker(hass, current_version)
        domain_data["update_checker"] = checker
    return checker


class CasaSmartUpdateStatusView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/update/status"
    name = f"api:{DOMAIN}:update:status"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, checker: UpdateChecker) -> None:
        self._hass = hass
        self._checker = checker

    async def get(self, request: web.Request) -> web.Response:
        """CasaSmart runtime component."""
        _, error = authenticate_request(self._hass, request, "update.read")
        if error is not None:
            return error
        return self.json(await self._checker.async_status())


class CasaSmartUpdateInstallView(HomeAssistantView):
    """CasaSmart runtime component."""

    url = f"/api/{DOMAIN}/update/install"
    name = f"api:{DOMAIN}:update:install"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, checker: UpdateChecker) -> None:
        self._hass = hass
        self._checker = checker

    async def post(self, request: web.Request) -> web.Response:
        _, error = authenticate_request(self._hass, request, "update.install")
        if error is not None:
            return error



        from .update import InstallError
        from .update_install import perform_install

        try:
            result = await perform_install(self._hass, self._checker)
        except InstallError as err:
            _LOGGER.warning("Self-update refused/failed: %s", err)
            return self.json({"error": str(err)}, status_code=HTTPStatus.CONFLICT)
        except Exception:
            _LOGGER.exception("Self-update crashed")
            return self.json(
                {"error": "internal error during install"},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return self.json(result, status_code=HTTPStatus.ACCEPTED)
