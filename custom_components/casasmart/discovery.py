"""CasaSmart runtime component."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)




SERVICE_TYPE = "_casasmart._tcp.local."


TXT_SCHEMA_VERSION = "1"


DEFAULT_HUB_NAME = "CasaSmart Hub"


@dataclass(frozen=True)
class MdnsServiceDescriptor:
    """CasaSmart runtime component."""

    service_type: str


    instance_name: str
    port: int

    properties: dict[str, bytes] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        """CasaSmart runtime component."""
        return f"{self.instance_name}.{self.service_type}"


def build_instance_name(hub_name: str | None, fingerprint: str) -> str:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    if not hub_id:
        raise ValueError("mDNS TXT 'id' (hub fingerprint) must be non-empty")
    records: dict[str, bytes] = {
        "id": hub_id.encode("utf-8"),
        "api": str(int(api_version)).encode("utf-8"),
        "v": TXT_SCHEMA_VERSION.encode("utf-8"),
    }
    cleaned_name = (hub_name or "").strip()
    if cleaned_name:


        records["name"] = cleaned_name[:63].encode("utf-8")
    return records


def build_service_descriptor(
    *,
    hub_id: str,
    hub_name: str | None,
    api_version: int,
    port: int,
) -> MdnsServiceDescriptor:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    return (fingerprint or "").strip().lower()[:8]


def _server_hostname(fingerprint: str) -> str:
    """CasaSmart runtime component."""
    short = _short_fingerprint(fingerprint) or "hub"
    return f"casasmart-{short}.local."





class MdnsAdvertiser:
    """CasaSmart runtime component."""

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
        self._info = None
        self._current_ip: str | None = None

    async def async_start(self) -> None:
        """CasaSmart runtime component."""
        try:
            from homeassistant.components import zeroconf as ha_zeroconf

            self._aiozc = await ha_zeroconf.async_get_async_instance(self._hass)
        except Exception as err:
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
        """CasaSmart runtime component."""
        if self._aiozc is None:

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
        except Exception as err:
            _LOGGER.warning("mDNS refresh failed: %s", err)
            return
        self._info = info
        self._current_ip = ip
        _LOGGER.info("mDNS record updated → %s:%d", ip or "(hostname)", self._port)

    async def async_stop(self) -> None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        try:
            from homeassistant.components import network

            ip = await network.async_get_source_ip(
                self._hass, network.MDNS_TARGET_IP
            )
            return ip
        except Exception as err:
            _LOGGER.debug("source IP lookup failed, hostname-only mDNS: %s", err)
            return None

    def _build_info(self, ip: str | None):
        """CasaSmart runtime component."""
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
