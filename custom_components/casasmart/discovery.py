"""Advertise the hub over mDNS (_casasmart._tcp) so the app finds it on the LAN.

The descriptor builders use the stdlib only, so tests import them without HA.
MdnsAdvertiser registers the record on HA's own zeroconf instance and follows
the hub's address when DHCP changes it.

TXT keys: id is the identity fingerprint the app pins, so a spoofed record
fails the TLS check; name is a display hint; api is the hub's API version; v is
the TXT schema version. Nothing in the record is secret.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# Descriptor builders: stdlib only, importable without HA.

# The service type the app browses for.
SERVICE_TYPE = "_casasmart._tcp.local."

# Bumped only when the meaning of a TXT key changes.
TXT_SCHEMA_VERSION = "1"

# Used when hub_config has no hub_name.
DEFAULT_HUB_NAME = "CasaSmart Hub"
# A TXT entry holds 255 bytes, "name=" included; 63 characters of a
# four-byte script would be 252.
_TXT_NAME_MAX_BYTES = 200


@dataclass(frozen=True)
class MdnsServiceDescriptor:
    """What the advertiser registers, independent of zeroconf."""

    service_type: str
    # The instance label alone, e.g. "CasaSmart Hub (1a2b3c4d)"; see full_name.
    instance_name: str
    port: int
    # TXT values as bytes, as zeroconf expects.
    properties: dict[str, bytes] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        """Fully qualified service name: <label>.<service_type>."""
        return f"{self.instance_name}.{self.service_type}"


def build_instance_name(hub_name: str | None, fingerprint: str) -> str:
    """The hub name plus a short fingerprint, unique per hub on the LAN.

    The suffix keeps two hubs with the same name apart without zeroconf's
    collision renaming. DNS caps a label at 63 bytes of UTF-8 and zeroconf
    refuses longer ones, so a long name is cut at a character boundary and
    the suffix always survives.
    """
    base = (hub_name or "").strip() or DEFAULT_HUB_NAME
    short = _short_fingerprint(fingerprint)
    suffix = f" ({short})" if short else ""
    return f"{_cut_utf8(base, 63 - len(suffix.encode('utf-8')))}{suffix}"


def _cut_utf8(text: str, max_bytes: int) -> str:
    """Cut text to at most max_bytes of UTF-8, on a character boundary."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").rstrip()


def build_txt_records(
    *,
    hub_id: str,
    hub_name: str | None,
    api_version: int,
) -> dict[str, bytes]:
    """Encode the TXT records; id is required and an empty name is left out."""
    if not hub_id:
        raise ValueError("mDNS TXT 'id' (hub fingerprint) must be non-empty")
    records: dict[str, bytes] = {
        "id": hub_id.encode("utf-8"),
        "api": str(int(api_version)).encode("utf-8"),
        "v": TXT_SCHEMA_VERSION.encode("utf-8"),
    }
    cleaned_name = (hub_name or "").strip()
    if cleaned_name:
        # Keep the record small whatever the configured name.
        name = _cut_utf8(cleaned_name[:63], _TXT_NAME_MAX_BYTES)
        records["name"] = name.encode("utf-8")
    return records


def build_service_descriptor(
    *,
    hub_id: str,
    hub_name: str | None,
    api_version: int,
    port: int,
) -> MdnsServiceDescriptor:
    """Assemble the descriptor; port is the hub's TLS listener, which the app dials."""
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
    """First 8 characters of the fingerprint, trimmed and lowercased."""
    return (fingerprint or "").strip().lower()[:8]


def _server_hostname(fingerprint: str) -> str:
    """A unique .local. hostname for the SRV and A records."""
    short = _short_fingerprint(fingerprint) or "hub"
    return f"casasmart-{short}.local."


# The advertiser imports HA and zeroconf lazily, inside its methods.


class MdnsAdvertiser:
    """Keeps the hub's record on HA's shared zeroconf instance.

    Sharing HA's instance avoids a second mDNS responder. Discovery is a
    convenience (the app also reaches the hub by its stored address or the
    tunnel), so every failure here is logged and swallowed. Once stopped it
    publishes nothing, even for a refresh that was already running.
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
        # Start, refresh and stop take turns, so a stop can't land mid-publish.
        self._lock = asyncio.Lock()
        self._stopped = False

    async def async_start(self) -> None:
        """Register the record under the hub's current LAN address."""
        async with self._lock:
            if not self._stopped:
                await self._async_start()

    async def _async_start(self) -> None:
        try:
            from homeassistant.components import zeroconf as ha_zeroconf

            self._aiozc = await ha_zeroconf.async_get_async_instance(self._hass)
        except Exception as err:
            _LOGGER.warning(
                "mDNS advertiser unavailable (zeroconf not ready): %s; the "
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
        except Exception as err:
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
        """Re-publish the record when DHCP has given the hub a new address."""
        async with self._lock:
            if not self._stopped:
                await self._async_refresh()

    async def _async_refresh(self) -> None:
        if self._aiozc is None:
            # Zeroconf was unavailable at setup; try a full start.
            await self._async_start()
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
        except Exception as err:
            _LOGGER.warning("mDNS refresh failed: %s", err)
            return
        self._info = info
        self._current_ip = ip
        _LOGGER.info("mDNS record updated → %s:%d", ip or "(hostname)", self._port)

    async def async_stop(self) -> None:
        """Unregister so other apps stop trying to reach a dead service."""
        self._stopped = True
        async with self._lock:
            if self._aiozc is None or self._info is None:
                return
            try:
                await self._aiozc.async_unregister_service(self._info)
            except Exception as err:
                _LOGGER.debug("mDNS unregister failed (harmless on shutdown): %s", err)
            finally:
                self._info = None
                self._current_ip = None

    async def _async_source_ip(self) -> str | None:
        """The hub's LAN-facing IPv4, or None to register hostname-only."""
        try:
            from homeassistant.components import network

            return await network.async_get_source_ip(self._hass, network.MDNS_TARGET_IP)
        except Exception as err:
            _LOGGER.debug("source IP lookup failed, hostname-only mDNS: %s", err)
            return None

    def _build_info(self, ip: str | None):
        """Build the zeroconf ServiceInfo, or None (logged) on failure."""
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
        except Exception as err:
            _LOGGER.warning("mDNS ServiceInfo build failed: %s", err)
            return None
