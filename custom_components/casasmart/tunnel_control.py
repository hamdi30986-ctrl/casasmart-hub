"""Cloudflare Tunnel add-on control through the Supervisor.

The setup reconciler (__init__.py) uses this to apply the tunnel switch:
enabled means started with boot=auto, disabled means stopped with
boot=manual, so a host reboot can't bring back a tunnel the owner turned off.
The edge watchdog restarts an add-on that runs but has lost Cloudflare.
Container and Core installs have no Supervisor: available() is False there
and the switch does nothing. The pure rules are in tunnel.py.
"""

from __future__ import annotations

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

# Container and Core installs may lack these. SupervisorError covers every
# client failure, connection errors and timeouts included.
try:
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import AddonBoot, AddonsOptions
    from homeassistant.components.hassio import get_supervisor_client
    from homeassistant.helpers.hassio import is_hassio

    _SUPERVISOR_AVAILABLE = True
except ImportError:  # pragma: no cover - installs without the hassio packages
    _SUPERVISOR_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)

# The same running states as tunnel.py.
_RUNNING_STATES = frozenset({"started", "startup"})

_EDGE_PROBE_TIMEOUT_SECONDS = 10.0


class TunnelControlError(HomeAssistantError):
    """A Supervisor add-on operation failed (wrapped SupervisorError)."""


@dataclass(frozen=True)
class TunnelAddonState:
    """Actual add-on state, as the reconciler compares it."""

    slug: str
    running: bool
    boot: str  # "auto" | "manual"


def _enum_value(value: object) -> str:
    """The string value of an AddonState or AddonBoot, or of a plain string."""
    return str(getattr(value, "value", value))


class CloudflaredController:
    """Finds, starts, stops and restarts the cloudflared add-on."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._slug: str | None = None
        # Monotonic time of the last watchdog restart, for the cooldown.
        self._last_edge_restart: float | None = None

    def available(self) -> bool:
        """True when a Supervisor exists (HAOS/Supervised installs only)."""
        return _SUPERVISOR_AVAILABLE and is_hassio(self._hass)

    def _addons(self):
        """The Supervisor add-ons client; callers check available() first."""
        return get_supervisor_client(self._hass).addons

    async def async_discover(self) -> str | None:
        """The cloudflared add-on slug, or None if none is installed.

        The pick is cached but checked against the live listing on every
        call, so the next reconcile notices an add-on removed or installed.
        """
        try:
            addons = await self._addons().list()
        except SupervisorError as err:
            raise TunnelControlError(
                f"Supervisor add-on listing failed: {err}"
            ) from err

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
        """The add-on's run state and boot mode."""
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
        """Set boot=auto, then start the add-on if it isn't running.

        Boot mode goes first, so if the start fails the add-on still comes
        up at the next host boot.
        """
        try:
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.AUTO)
            )
            if not running:
                await self._addons().start_addon(slug)
        except SupervisorError as err:
            raise TunnelControlError(f"Enabling add-on {slug} failed: {err}") from err
        _LOGGER.info("Cloudflare tunnel add-on %s enabled (started, boot=auto)", slug)

    async def async_disable(self, slug: str, *, running: bool) -> None:
        """Stop the add-on if it is running, then set boot=manual.

        Stopping goes first so remote access ends at once; a failed boot-mode
        write is retried on the next reconcile.
        """
        try:
            if running:
                await self._addons().stop_addon(slug)
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.MANUAL)
            )
        except SupervisorError as err:
            raise TunnelControlError(f"Disabling add-on {slug} failed: {err}") from err
        _LOGGER.info(
            "Cloudflare tunnel add-on %s disabled (stopped, boot=manual)", slug
        )

    async def async_edge_alive(self, tunnel_url: str) -> bool | None:
        """Probe the public tunnel URL through Cloudflare's edge.

        The request goes out to Cloudflare and back through the tunnel, which
        shows whether cloudflared is connected. True if the reply came through
        the tunnel, False if Cloudflare reports the tunnel down, None if
        nothing answered (usually the hub's own internet is down).
        """
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
        except (TimeoutError, aiohttp.ClientError):
            return None

    async def async_restart(self, slug: str) -> None:
        """Restart the add-on so cloudflared re-establishes its edge tunnel."""
        try:
            await self._addons().restart_addon(slug)
        except SupervisorError as err:
            raise TunnelControlError(f"Restarting add-on {slug} failed: {err}") from err
        _LOGGER.info("Cloudflare tunnel add-on %s restarted (edge reconnect)", slug)

    async def async_watchdog_check(self, slug: str, tunnel_url: str, now: float) -> str:
        """One watchdog cycle: probe, decide and restart if needed; return the verdict.

        now is a monotonic time. A failed restart raises TunnelControlError
        without starting the cooldown, so the next cycle retries.
        """
        alive = await self.async_edge_alive(tunnel_url)
        decision = edge_watchdog_decision(alive, self._last_edge_restart, now)
        if decision == "restart":
            await self.async_restart(slug)
            self._last_edge_restart = now
        return decision

    async def async_restore_boot_auto(self, slug: str) -> None:
        """Set boot=auto again when the hub stops managing the add-on.

        Called when the Cloudflare domain is cleared or the integration is
        removed. It doesn't start the add-on: giving up control isn't consent
        to open remote access now.
        """
        try:
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.AUTO)
            )
        except SupervisorError as err:
            raise TunnelControlError(
                f"Restoring boot=auto on add-on {slug} failed: {err}"
            ) from err
