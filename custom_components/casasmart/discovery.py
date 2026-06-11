"""mDNS advertiser (B6 — Multi-Home / Layer-0 network discovery).

The hub broadcasts ``_casasmart._tcp`` on the LAN so the app finds it
regardless of DHCP — no static IP, no GPS, like AirPlay finding an Apple
TV (plan B6 / Layer 0 "Network Discovery").

Two layers, same split the rest of the integration uses:

* The **pure builder** (this module's top half) turns hub identity +
  network facts into a transport-neutral :class:`MdnsServiceDescriptor`
  — instance name, port, TXT records. Stdlib only, zero HA/zeroconf
  imports, so ``tests/test_discovery.py`` imports it directly (the
  package ``__init__`` pulls in ``homeassistant``, absent in the test
  env). It is also what the ``.dev`` LAN proof harness reuses to publish
  the genuine service from the host (Docker Desktop on macOS can't
  multicast a container onto the LAN — the real HAOS/LXC hub does).

* :class:`MdnsAdvertiser` (bottom half) is the thin lifecycle wrapper:
  it reuses **HA's own zeroconf instance** (no second mDNS responder
  fighting the first, no new dependency — 15-yr-stability filter),
  registers the service at setup, re-publishes the address when the
  hub's LAN IP shifts, and unregisters on unload. Its HA/zeroconf
  imports are lazy (inside methods) so the pure half stays importable
  without HA.

TXT contract (kept tiny — one UDP packet, forward-compatible):

==========  ====================================================
key         value
==========  ====================================================
``id``      the hub's permanent identity fingerprint (SHA-256 hex
            over the B10 identity SPKI). The multi-home matching
            key AND the B10 pin — an attacker can spoof the TXT id
            but not the TLS identity behind it, so the pin catches
            a liar. REQUIRED; a record without it is unusable.
``name``    friendly hub name (display hint only; the app's paired
            record name wins). Optional.
``api``     API version the hub speaks (handshake hint, lets the
            app skip an incompatible hub before connecting).
``v``       TXT schema version, so adding a field later never
            breaks an old parser.
==========  ====================================================

The TXT carries no secret — the identity *public* key and the real
trust decision happen at the handshake/TLS layer (B10/B11), never here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# ── Pure builder (stdlib only — keep it HA/zeroconf-free) ──

#: The service type the app browses for (plan: ``_casasmart._tcp``).
SERVICE_TYPE = "_casasmart._tcp.local."

#: TXT schema version — bumped only if the *meaning* of a key changes.
TXT_SCHEMA_VERSION = "1"

#: Default friendly name when the installer hasn't set ``hub_name``.
DEFAULT_HUB_NAME = "CasaSmart Hub"


@dataclass(frozen=True)
class MdnsServiceDescriptor:
    """Everything needed to register the service, transport-neutral.

    The lifecycle wrapper converts this into a ``zeroconf.ServiceInfo``;
    the proof harness converts it the same way. Neither the descriptor
    nor its builder import zeroconf, so both stay unit-testable.
    """

    service_type: str
    #: The instance label only (e.g. "CasaSmart Hub (ab6ebef6)"), NOT the
    #: fully-qualified "<label>.<service_type>".
    instance_name: str
    port: int
    #: TXT records as bytes (zeroconf/mDNS values are opaque binary).
    properties: dict[str, bytes] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        """Fully-qualified service name ``<label>.<service_type>``."""
        return f"{self.instance_name}.{self.service_type}"


def build_instance_name(hub_name: str | None, fingerprint: str) -> str:
    """A stable, unique-by-construction instance label.

    Appending the short fingerprint makes two hubs that share a name
    (both default "CasaSmart Hub") distinct on one LAN without relying on
    zeroconf's "(2)"/"(3)" collision rename — and ties the visible label
    to the crypto identity. A label is capped at 63 bytes by DNS; ours is
    far under, but the cap is honoured defensively.
    """
    base = (hub_name or "").strip() or DEFAULT_HUB_NAME
    short = _short_fingerprint(fingerprint)
    label = f"{base} ({short})" if short else base
    return label[:63]


def build_txt_records(
    *,
    hub_id: str,
    hub_name: str | None,
    api_version: int,
) -> dict[str, bytes]:
    """Encode the TXT contract. ``id`` is mandatory; ``name`` is dropped
    when unset rather than published empty."""
    if not hub_id:
        raise ValueError("mDNS TXT 'id' (hub fingerprint) must be non-empty")
    records: dict[str, bytes] = {
        "id": hub_id.encode("utf-8"),
        "api": str(int(api_version)).encode("utf-8"),
        "v": TXT_SCHEMA_VERSION.encode("utf-8"),
    }
    cleaned_name = (hub_name or "").strip()
    if cleaned_name:
        # TXT values SHOULD stay small; a pathologically long name can't
        # bloat the packet.
        records["name"] = cleaned_name[:63].encode("utf-8")
    return records


def build_service_descriptor(
    *,
    hub_id: str,
    hub_name: str | None,
    api_version: int,
    port: int,
) -> MdnsServiceDescriptor:
    """Assemble the full descriptor from hub identity + the LAN port.

    ``port`` is the secure CasaSmart API port (the TLS listener, B10/B11
    — LAN traffic is pinned-TLS), i.e. what the app should actually dial.
    """
    if port <= 0 or port > 65535:
        raise ValueError(f"invalid mDNS port {port}")
    return MdnsServiceDescriptor(
        service_type=SERVICE_TYPE,
        instance_name=build_instance_name(hub_name, hub_id),
        port=port,
        properties=build_txt_records(
            hub_id=hub_id, hub_name=hub_name, api_version=api_version
        ),
    )


def _short_fingerprint(fingerprint: str) -> str:
    """First 8 hex chars of the fingerprint, defensively cleaned."""
    return (fingerprint or "").strip().lower()[:8]


def _server_hostname(fingerprint: str) -> str:
    """A unique ``.local.`` hostname for the SRV/A records."""
    short = _short_fingerprint(fingerprint) or "hub"
    return f"casasmart-{short}.local."


# ── Lifecycle wrapper (HA + zeroconf; lazy imports keep the top half pure) ──


class MdnsAdvertiser:
    """Registers/refreshes/unregisters the hub's ``_casasmart._tcp``
    record on HA's shared zeroconf instance.

    Graceful degradation (quality filter 3): mDNS is a *discovery
    convenience*, never load-bearing — the app still reaches the hub via
    the stored IP and the tunnel (B16 chain). So every failure here is
    logged and swallowed; it must never take the integration down.
    """

    def __init__(
        self,
        hass,
        *,
        hub_id: str,
        hub_name: str | None,
        api_version: int,
        port: int,
    ) -> None:
        self._hass = hass
        self._hub_id = hub_id
        self._hub_name = hub_name
        self._api_version = api_version
        self._port = port
        self._descriptor = build_service_descriptor(
            hub_id=hub_id,
            hub_name=hub_name,
            api_version=api_version,
            port=port,
        )
        self._aiozc = None
        self._info = None  # zeroconf.ServiceInfo once registered
        self._current_ip: str | None = None

    async def async_start(self) -> None:
        """Resolve the LAN IP, build the ServiceInfo, register it."""
        try:
            from homeassistant.components import zeroconf as ha_zeroconf

            self._aiozc = await ha_zeroconf.async_get_async_instance(self._hass)
        except Exception as err:  # noqa: BLE001 — never fail setup over mDNS
            _LOGGER.warning(
                "mDNS advertiser unavailable (zeroconf not ready): %s — the "
                "app will still reach the hub via stored IP / tunnel",
                err,
            )
            self._aiozc = None
            return

        ip = await self._async_source_ip()
        info = self._build_info(ip)
        if info is None:
            return
        try:
            await self._aiozc.async_register_service(info)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("mDNS register failed: %s", err)
            return
        self._info = info
        self._current_ip = ip
        _LOGGER.info(
            "mDNS advertising %s on %s:%d (id=%s)",
            self._descriptor.instance_name,
            ip or "(hostname only)",
            self._port,
            _short_fingerprint(self._hub_id),
        )

    async def async_refresh(self, _now=None) -> None:
        """Re-publish the address when the hub's LAN IP has shifted.

        DHCP can hand the hub a new IP at any lease renewal; the plan
        requires the mDNS record to follow it ("Integration stores its
        own current IP and updates mDNS records on change"). A no-op when
        the IP is unchanged so the periodic tick is nearly free.
        """
        if self._aiozc is None:
            # Setup-time zeroconf failure — try a full (re)start instead.
            await self.async_start()
            return
        ip = await self._async_source_ip()
        if ip == self._current_ip and self._info is not None:
            return
        info = self._build_info(ip)
        if info is None:
            return
        try:
            if self._info is None:
                await self._aiozc.async_register_service(info)
            else:
                await self._aiozc.async_update_service(info)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("mDNS refresh failed: %s", err)
            return
        self._info = info
        self._current_ip = ip
        _LOGGER.info("mDNS record updated → %s:%d", ip or "(hostname)", self._port)

    async def async_stop(self) -> None:
        """Unregister so other apps stop trying to reach a dead service."""
        if self._aiozc is None or self._info is None:
            return
        try:
            await self._aiozc.async_unregister_service(self._info)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("mDNS unregister failed (harmless on shutdown): %s", err)
        finally:
            self._info = None
            self._current_ip = None

    # ── internals ──

    async def _async_source_ip(self) -> str | None:
        """The hub's LAN-facing IPv4, or None to register hostname-only."""
        try:
            from homeassistant.components import network

            ip = await network.async_get_source_ip(
                self._hass, network.MDNS_TARGET_IP
            )
            return ip
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("source IP lookup failed, hostname-only mDNS: %s", err)
            return None

    def _build_info(self, ip: str | None):
        """Construct a ``zeroconf.ServiceInfo`` from the descriptor + IP."""
        try:
            import socket

            from zeroconf import ServiceInfo

            addresses = []
            if ip:
                try:
                    addresses = [socket.inet_aton(ip)]
                except OSError:
                    addresses = []
            return ServiceInfo(
                type_=self._descriptor.service_type,
                name=self._descriptor.full_name,
                addresses=addresses,
                port=self._descriptor.port,
                properties=dict(self._descriptor.properties),
                server=_server_hostname(self._hub_id),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("mDNS ServiceInfo build failed: %s", err)
            return None
