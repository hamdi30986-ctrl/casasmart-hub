"""Which listener may vouch for "this client is on the LAN" (pairing, recovery).

Pairing, owner recovery and keyless speaker provisioning are LAN-only. On most
hubs the TLS listener (``tls.py``) sees the phone's real address, so the LAN
gate (``auth_api.is_lan_request``) simply checks that address.

Docker Desktop (macOS, Windows) is different: every connection published into
its VM arrives with a synthetic source address, and which one is not stable.
Docker Desktop 4.68 hands out arbitrary public addresses after a container
restart, where it used to show the bridge gateway (``172.18.0.1``). On
those hosts the documented setup (``deploy/macos``) publishes the TLS port to
``127.0.0.1`` only and fronts it with a byte-for-byte relay that admits only
LAN clients — so on Docker Desktop, *arriving on the TLS listener* is the LAN
proof, and the rewritten address is not.

``lan_relay_ingress`` in hub config selects the policy:

- ``"auto"`` (default): trust the TLS listener as LAN when running under Docker
  Desktop (its VM kernel identifies as ``linuxkit``), else check addresses.
- ``"on"``: always trust the TLS listener as LAN (another relay-only setup).
- ``"off"``: always check addresses (the behavior before 2.3.0).

Requests that crossed Cloudflare are never LAN whatever this says (checked
first in ``is_lan_request``), and HA's own HTTP port is never trusted this way:
the tunnel enters there.

Pure (stdlib only), so the unit tests import it directly. The listener marker
itself lives in ``tls.py`` (``TLS_LISTENER_TRUSTED_LAN``), next to the listener.
"""

from __future__ import annotations

LAN_RELAY_INGRESS_CONFIG_KEY = "lan_relay_ingress"
LAN_RELAY_INGRESS_AUTO = "auto"
LAN_RELAY_INGRESS_ON = "on"
LAN_RELAY_INGRESS_OFF = "off"
_LAN_RELAY_INGRESS_VALUES = (
    LAN_RELAY_INGRESS_AUTO,
    LAN_RELAY_INGRESS_ON,
    LAN_RELAY_INGRESS_OFF,
)


def is_recognized_lan_relay_ingress(setting: object) -> bool:
    """True for a value :func:`resolve_lan_relay_ingress` understands (unset counts)."""
    return (
        setting is None
        or isinstance(setting, bool)
        or setting in _LAN_RELAY_INGRESS_VALUES
    )


def is_docker_desktop_kernel(proc_version: str | None) -> bool:
    """True for the Docker Desktop VM kernel (``/proc/version`` says linuxkit)."""
    return isinstance(proc_version, str) and "linuxkit" in proc_version.lower()


def resolve_lan_relay_ingress(
    setting: object, proc_version: str | None
) -> tuple[bool, str]:
    """``(trusted, reason)`` for the hub's TLS listener, from the hub config value.

    Never raises: an unrecognized value falls back to ``"auto"`` (and says so in
    the reason, which the caller logs).
    """
    if setting is True or setting == LAN_RELAY_INGRESS_ON:
        return True, "lan_relay_ingress is on"
    if setting is False or setting == LAN_RELAY_INGRESS_OFF:
        return False, "lan_relay_ingress is off"
    note = ""
    if setting not in (None, LAN_RELAY_INGRESS_AUTO):
        note = f" (unrecognized lan_relay_ingress {setting!r}; using auto)"
    if is_docker_desktop_kernel(proc_version):
        return True, "Docker Desktop detected" + note
    return False, "clients' own addresses are visible" + note
