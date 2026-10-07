"""Which listener may vouch for "this client is on the LAN" (pairing, recovery).

Pairing, owner recovery and keyless speaker provisioning are LAN-only. On most
hubs the TLS listener (``tls.py``) sees the phone's real address, so the LAN
gate (``auth_api.is_lan_request``) simply checks that address.

Docker Desktop is different: every connection published into its VM arrives
with a synthetic source address, and which one is not stable (after a
container restart Docker Desktop 4.68 can show an arbitrary public address
instead of the bridge gateway ``172.18.0.1``). There the documented setup
(``deploy/macos``) publishes the TLS port to ``127.0.0.1`` only and fronts it
with a byte-for-byte relay that admits only LAN clients, so *arriving on the
TLS listener* is the LAN proof and the rewritten address is not.

The hub can't verify that setup from inside the container, so trusting the
listener is an explicit operator choice. ``lan_relay_ingress`` in hub config:

- ``"off"`` (the default, also when unset): check client addresses.
- ``"on"``: trust the TLS listener as LAN. Only for a setup where the TLS port
  is reachable through a LAN-only relay and nothing else.

When the hub runs under Docker Desktop (its VM kernel identifies as
``linuxkit``) with the setting unset, setup logs a warning that says what to
set: there the address check judges an address Docker Desktop makes up (its
private gateway after some restarts, an arbitrary public address after
others), so LAN pairing works or fails unpredictably. On Docker Desktop it is
the loopback-only publish plus the relay that keeps the port off the internet,
whatever this setting says.

Requests that crossed Cloudflare are never LAN whatever this says (checked
first in ``is_lan_request``), and HA's own HTTP port is never trusted this way:
the tunnel enters there.

Pure (stdlib only), so the unit tests import it directly. The listener marker
itself lives in ``tls.py`` (``TLS_LISTENER_TRUSTED_LAN``), next to the listener.
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
    """True for the Docker Desktop VM kernel (``/proc/version`` says linuxkit)."""
    return isinstance(proc_version, str) and "linuxkit" in proc_version.lower()


def resolve_lan_relay_ingress(setting: object) -> bool:
    """Whether the hub's TLS listener counts as LAN: only an explicit "on"."""
    return setting is True or setting == LAN_RELAY_INGRESS_ON


def needs_relay_ingress_hint(setting: object, proc_version: str | None) -> bool:
    """True on Docker Desktop when the operator hasn't chosen on or off.

    There the address check judges made-up addresses, so LAN pairing works or
    fails unpredictably until the operator chooses; setup says what to set.
    """
    return is_docker_desktop_kernel(proc_version) and not _is_explicit(setting)
