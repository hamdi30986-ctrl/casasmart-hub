"""Cloudflare tunnel add-on control — the Supervisor-coupled half.

``tunnel.py`` owns the pure logic (domain validation, slug matching); this
module is the thin HA/Supervisor glue that enforces the desired tunnel
state recorded in the config entry's options (``__init__.py`` reconciler):

    enabled  -> add-on started + boot=auto   (ON survives host reboots)
    disabled -> add-on stopped + boot=manual (OFF survives host reboots)

``boot=manual`` is the point of the disable leg: a HAOS host reboot must
not resurrect a tunnel the owner turned off for pairing — the Supervisor
itself keeps it down, not just our reconciler.

Only the Supervisor backend exists, deliberately: the production fleet is
HAOS + the cloudflared add-on, and a Container/Core install cannot reach
host systemd from inside HA anyway. On those installs ``available()`` is
False and every caller degrades gracefully — domain storage + handshake
advertising keep working, the options toggle is inert with a clear message
(same doctrine as mDNS/push: one feature degrades, the hub stays up).

The add-on slug is repo-hash-prefixed and NOT stable across add-on repos
(observed ``9074a9fa_cloudflared``; the add-on just migrated repos, so
future installs may differ again) — it is discovered at runtime from the
live add-on listing, never hardcoded.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .tunnel import (
    EDGE_RESTART_COOLDOWN_SECONDS,
    edge_watchdog_decision,
    is_edge_origin_down,
    pick_cloudflared_slug,
)

# Supervisor plumbing is optional at runtime (Container/Core installs) —
# guard the imports so merely importing this module can never fail there.
# aiohasupervisor wraps its connection/timeout failures in SupervisorError
# subclasses, so that one except-type covers the whole client surface.
try:
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import AddonBoot, AddonsOptions
    from homeassistant.components.hassio import get_supervisor_client
    from homeassistant.helpers.hassio import is_hassio

    _SUPERVISOR_AVAILABLE = True
except ImportError:  # pragma: no cover — pip installs without hassio deps
    _SUPERVISOR_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)

# Mirrors tunnel.py's running-state set; kept here too so the comparison the
# reconciler makes (against .value strings) is explicit at the usage site.
_RUNNING_STATES = frozenset({"started", "startup"})

# --- Edge-liveness watchdog (Phase 9) ---------------------------------------
# The pure logic (which statuses mean origin-down, and the probe/restart
# decision) lives in tunnel.py so it's unit-testable without HA. Only the
# HTTP-probe timeout is glue and stays here.
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
    """AddonState/AddonBoot enum -> its wire string (tolerant of plain str)."""
    return str(getattr(value, "value", value))


class CloudflaredController:
    """Discover + start/stop the cloudflared add-on via the Supervisor."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._slug: str | None = None
        # Monotonic timestamp of the last edge-driven restart (cooldown gate).
        self._last_edge_restart: float | None = None

    def available(self) -> bool:
        """True when a Supervisor exists (HAOS/Supervised installs only)."""
        return _SUPERVISOR_AVAILABLE and is_hassio(self._hass)

    def _addons(self):
        """The Supervisor addons client (callers hold the available() gate)."""
        return get_supervisor_client(self._hass).addons

    async def async_discover(self) -> str | None:
        """Return the cloudflared add-on slug, or None if none is installed.

        The pick is cached, but re-verified against the live listing on
        every call — an uninstalled add-on drops the cache, installing one
        later "just works" on the next reconcile.
        """
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
        """The add-on's actual run state + boot mode."""
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
        """Enact ON: boot=auto, then start if not already running.

        Boot mode first — if the start then fails, the add-on still comes
        up on the next host reboot, which is the enabled contract.
        """
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
        """Enact OFF: stop if running, then boot=manual.

        Stop first — the immediate problem (tunnel routing pairing traffic)
        ends now; if the boot-mode write then fails, the reconciler retries
        it on the next run and logs meanwhile.
        """
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
        """Probe the public tunnel URL through Cloudflare's edge (Phase 9).

        The request loops hub -> Cloudflare edge -> tunnel -> hub, so it tests
        the thing the add-on's ``running`` state can't: is cloudflared actually
        connected to the edge? Returns True if a response came back through the
        tunnel, False if Cloudflare reports the origin unreachable (restart), or
        None if there was no response at all — which usually means the hub's own
        internet/DNS is down, NOT the tunnel, so the caller must not restart.
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
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def async_restart(self, slug: str) -> None:
        """Restart the add-on so cloudflared re-establishes its edge tunnel."""
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
        """One edge-liveness cycle: probe, decide, and restart if warranted.

        [now] is a monotonic timestamp (injected for testability). Returns the
        [edge_watchdog_decision] verdict; on ``"restart"`` the add-on is
        restarted and the cooldown clock is stamped.
        """
        alive = await self.async_edge_alive(tunnel_url)
        decision = edge_watchdog_decision(alive, self._last_edge_restart, now)
        if decision == "restart":
            await self.async_restart(slug)
            self._last_edge_restart = now
        return decision

    async def async_restore_boot_auto(self, slug: str) -> None:
        """Hand auto-boot back to the Supervisor (integration removal).

        Deliberately does NOT start the add-on — removal is not consent to
        open remote access right now, only to stop pinning it down.
        """
        try:
            await self._addons().set_addon_options(
                slug, AddonsOptions(boot=AddonBoot.AUTO)
            )
        except SupervisorError as err:
            raise TunnelControlError(
                f"Restoring boot=auto on add-on {slug} failed: {err}"
            ) from err
