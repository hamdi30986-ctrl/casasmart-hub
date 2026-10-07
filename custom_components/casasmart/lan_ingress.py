"""Whether the hub's TLS listener counts as LAN for pairing and recovery.

The LAN gate (auth_api.is_lan_request) normally checks the client address.
Docker Desktop replaces that address with one that changes between restarts,
so the documented macOS setup publishes the TLS port to 127.0.0.1 behind a
relay that admits only LAN clients. The hub can't verify that setup from inside
the container, so trusting the listener takes an explicit "lan_relay_ingress":
"on" in hub config; unset or "off" keeps the address check. See the README.
"""

from __future__ import annotations

LAN_RELAY_INGRESS_CONFIG_KEY = "lan_relay_ingress"
LAN_RELAY_INGRESS_ON = "on"
LAN_RELAY_INGRESS_OFF = "off"


def _is_explicit(setting: object) -> bool:
    """An operator's explicit choice: "on", "off" or a JSON boolean."""
    return isinstance(setting, bool) or setting in (
        LAN_RELAY_INGRESS_ON,
        LAN_RELAY_INGRESS_OFF,
    )


def is_recognized_lan_relay_ingress(setting: object) -> bool:
    """True for a value this module understands (unset counts)."""
    return setting is None or _is_explicit(setting)


def is_docker_desktop_kernel(proc_version: str | None) -> bool:
    """True for the Docker Desktop VM kernel (its /proc/version says linuxkit)."""
    return isinstance(proc_version, str) and "linuxkit" in proc_version.lower()


def resolve_lan_relay_ingress(setting: object) -> bool:
    """Whether the TLS listener counts as LAN: "on" or JSON true."""
    return setting is True or setting == LAN_RELAY_INGRESS_ON


def needs_relay_ingress_hint(setting: object, proc_version: str | None) -> bool:
    """True on Docker Desktop while the operator hasn't chosen on or off.

    The address check sees made-up addresses there, so LAN pairing is
    unreliable until the operator chooses; setup logs what to set.
    """
    return is_docker_desktop_kernel(proc_version) and not _is_explicit(setting)
