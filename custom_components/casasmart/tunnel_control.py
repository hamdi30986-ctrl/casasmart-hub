"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .tunnel import (
    edge_watchdog_decision,
    is_edge_origin_down,
    pick_cloudflared_slug,
)





try:
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import AddonBoot, AddonsOptions
    from homeassistant.components.hassio import get_supervisor_client
    from homeassistant.helpers.hassio import is_hassio

    _SUPERVISOR_AVAILABLE = True
except ImportError:
    _SUPERVISOR_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)



_RUNNING_STATES = frozenset({"started", "startup"})





_EDGE_PROBE_TIMEOUT_SECONDS = 10.0


class TunnelControlError(HomeAssistantError):
    """CasaSmart runtime component."""


@dataclass(frozen=True)
class TunnelAddonState:
    """CasaSmart runtime component."""

    slug: str
    running: bool
    boot: str


def _enum_value(value: object) -> str:
    """CasaSmart runtime component."""
    return str(getattr(value, "value", value))


class CloudflaredController:
    """CasaSmart runtime component."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._slug: str | None = None

        self._last_edge_restart: float | None = None

    def available(self) -> bool:
        """CasaSmart runtime component."""
        return _SUPERVISOR_AVAILABLE and is_hassio(self._hass)

    def _addons(self):
        """CasaSmart runtime component."""
        return get_supervisor_client(self._hass).addons

    async def async_discover(self) -> str | None:
        """CasaSmart runtime component."""
        try:
            addons = await self._addons().list()
        except SupervisorError as err:
            raise TunnelControlError(f"Supervisor add-on listing failed: {err}") from err

        listing = [
            (addon.slug, addon.name, _enum_value(addon.state)) for addon in addons
        ]
        slugs = {slug for slug, _name, _state in listing}
        if self._slug is not None and self._slug in slugs:
            return self._slug

        picked = pick_cloudflared_slug(listing)
        matches = sorted(
            slug
            for slug in slugs
            if slug == "cloudflared" or slug.endswith("_cloudflared")
        )
        if picked is not None and len(matches) > 1:
            _LOGGER.info(
                "Multiple cloudflared add-ons installed %s — controlling %s",
                matches,
                picked,
            )
        self._slug = picked
        return picked

    async def async_state(self, slug: str) -> TunnelAddonState:
        """CasaSmart runtime component."""
        try:
            info = await self._addons().addon_info(slug)
        except SupervisorError as err:
            raise TunnelControlError(
                f"Supervisor info for add-on {slug} failed: {err}"
            ) from err
        return TunnelAddonState(
            slug=slug,
            running=_enum_value(info.state) in _RUNNING_STATES,
            boot=_enum_value(info.boot),
        )

    async def async_enable(self, slug: str, *, running: bool) -> None:
        """CasaSmart runtime component."""
        try:
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.AUTO)
            )
            if not running:
                await self._addons().start_addon(slug)
        except SupervisorError as err:
            raise TunnelControlError(
                f"Enabling add-on {slug} failed: {err}"
            ) from err
        _LOGGER.info(
            "Cloudflare tunnel add-on %s enabled (started, boot=auto)", slug
        )

    async def async_disable(self, slug: str, *, running: bool) -> None:
        """CasaSmart runtime component."""
        try:
            if running:
                await self._addons().stop_addon(slug)
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.MANUAL)
            )
        except SupervisorError as err:
            raise TunnelControlError(
                f"Disabling add-on {slug} failed: {err}"
            ) from err
        _LOGGER.info(
            "Cloudflare tunnel add-on %s disabled (stopped, boot=manual)", slug
        )

    async def async_edge_alive(self, tunnel_url: str) -> bool | None:
        """CasaSmart runtime component."""
        session = async_get_clientsession(self._hass)
        timeout = aiohttp.ClientTimeout(total=_EDGE_PROBE_TIMEOUT_SECONDS)
        try:
            async with session.get(
                tunnel_url,
                timeout=timeout,
                allow_redirects=False,
                headers={"user-agent": "casasmart-edge-watchdog"},
            ) as resp:
                return not is_edge_origin_down(resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def async_restart(self, slug: str) -> None:
        """CasaSmart runtime component."""
        try:
            await self._addons().restart_addon(slug)
        except SupervisorError as err:
            raise TunnelControlError(
                f"Restarting add-on {slug} failed: {err}"
            ) from err
        _LOGGER.info(
            "Cloudflare tunnel add-on %s restarted (edge reconnect)", slug
        )

    async def async_watchdog_check(
        self, slug: str, tunnel_url: str, now: float
    ) -> str:
        """CasaSmart runtime component."""
        alive = await self.async_edge_alive(tunnel_url)
        decision = edge_watchdog_decision(alive, self._last_edge_restart, now)
        if decision == "restart":
            await self.async_restart(slug)
            self._last_edge_restart = now
        return decision

    async def async_restore_boot_auto(self, slug: str) -> None:
        """CasaSmart runtime component."""
        try:
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.AUTO)
            )
        except SupervisorError as err:
            raise TunnelControlError(
                f"Restoring boot=auto on add-on {slug} failed: {err}"
            ) from err
