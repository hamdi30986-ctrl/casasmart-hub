"""Push relay settings: validation, endpoints, reloads and migration.

The relay base URL is a config-entry option (``push_relay_url``). The
single-use hub activation code and the id of the request that submitted it
sit in the entry's data until registration succeeds. These helpers keep the
config flow (``config_flow.py``) and setup (``__init__.py``) in agreement:

- ``normalize_relay_base_url`` accepts only a public HTTPS origin;
- ``relay_endpoints`` derives the push and registration URLs from it;
- ``relay_config_snapshot`` / ``relay_reload_required`` decide whether an
  options change needs the push side restarted;
- ``quiesce_relay_runtime`` / ``async_reload_relay_runtime`` stop it and
  reload the entry;
- ``migrate_relay_options`` moves a relay URL that older versions kept in
  ``hub_config.json`` into the entry options.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .const import (
    CONF_PUSH_RELAY_URL,
    CONF_RELAY_ACTIVATION_CODE,
    CONF_RELAY_ACTIVATION_REQUEST_ID,
    PUSH_RELAY_PUSH_PATH,
    PUSH_RELAY_REGISTRATION_PATH,
)

# One DNS label: 1-63 chars of [a-z0-9-], no leading or trailing hyphen.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Names that only resolve on a private network, never a production relay.
_NON_PRODUCTION_SUFFIXES = (".internal", ".local", ".localhost")


def normalize_relay_base_url(value: object) -> str | None:
    """The canonical relay origin (``https://host[:port]``), or None.

    The relay receives every push and the hub's registration, so only a
    public production origin passes: ``https``, printable ASCII, no
    credentials, path, query or fragment, and a DNS name of two or more
    labels (no IP literal, no ``.local``/``.internal``/``.localhost``, no
    numeric top-level label). The host is lowercased and port 443 dropped.
    """
    if not isinstance(value, str):
        return None

    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    raw = value.strip(" ")
    if not raw or "\\" in raw:
        return None
    if any(ord(char) > 0x7E or char.isspace() for char in raw):
        return None

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if port == 0:
        return None

    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None

    hostname = parsed.hostname
    if hostname is None:
        return None
    hostname = hostname.lower()
    if hostname.endswith(".") or len(hostname) > 253:
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None

    labels = hostname.split(".")
    if (
        len(labels) < 2
        or hostname.endswith(_NON_PRODUCTION_SUFFIXES)
        or labels[-1].isdigit()
        or any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels)
    ):
        return None

    netloc = hostname if port in (None, 443) else f"{hostname}:{port}"
    return f"https://{netloc}"


@dataclass(frozen=True)
class RelayEndpoints:
    """The relay URLs the hub calls, derived from one validated origin."""

    base_url: str | None
    push_url: str
    registration_url: str


def relay_endpoints(base_url: object) -> RelayEndpoints:
    """The push and registration URLs; ValueError for an invalid origin."""
    normalized = normalize_relay_base_url(base_url)
    if normalized is None:
        raise ValueError("relay base URL is not a valid production HTTPS origin")
    return RelayEndpoints(
        base_url=normalized,
        push_url=normalized + PUSH_RELAY_PUSH_PATH,
        registration_url=normalized + PUSH_RELAY_REGISTRATION_PATH,
    )


@dataclass(frozen=True)
class RelayConfigSnapshot:
    """The relay settings a push side runs with, or is asked to run with."""

    base_url: str | None
    activation_request_id: str | None
    has_activation_code: bool


def relay_config_snapshot(
    options: Mapping[str, Any], data: Mapping[str, Any]
) -> RelayConfigSnapshot:
    """Read the relay settings from a config entry's options and data."""
    base_url = normalize_relay_base_url(options.get(CONF_PUSH_RELAY_URL))
    request_id = data.get(CONF_RELAY_ACTIVATION_REQUEST_ID)
    if not isinstance(request_id, str) or not request_id:
        request_id = None
    activation = data.get(CONF_RELAY_ACTIVATION_CODE)
    return RelayConfigSnapshot(
        base_url=base_url,
        activation_request_id=request_id,
        has_activation_code=isinstance(activation, str) and bool(activation.strip()),
    )


def relay_reload_required(
    applied: RelayConfigSnapshot, requested: RelayConfigSnapshot
) -> bool:
    """True when the origin changed or a fresh activation code was submitted.

    Each submitted code gets a new request id, so the same code left in the
    entry data doesn't trigger a second reload.
    """
    return applied.base_url != requested.base_url or (
        requested.has_activation_code
        and requested.activation_request_id != applied.activation_request_id
    )


def quiesce_relay_runtime(runtime_data: Any) -> None:
    """Stop and forget the tank monitor, dispatcher and registrar.

    Runs before a relay reload, so a reload that fails leaves push stopped
    rather than still sending to the old relay. Synchronous, so the options
    flow can call it right after saving.
    """
    if runtime_data is None:
        return
    tank_monitor = getattr(runtime_data, "tank_push_monitor", None)
    if tank_monitor is not None:
        tank_monitor.async_stop()
        runtime_data.tank_push_monitor = None
    dispatcher = getattr(runtime_data, "push_dispatcher", None)
    if dispatcher is not None:
        dispatcher.async_stop()
        runtime_data.push_dispatcher = None
    registrar = getattr(runtime_data, "relay_registrar", None)
    if registrar is not None:
        registrar.stop()
        runtime_data.relay_registrar = None


async def async_reload_relay_runtime(hass: Any, entry: Any) -> bool:
    """Quiesce the push side, then reload the entry; False if that failed."""
    quiesce_relay_runtime(getattr(entry, "runtime_data", None))
    try:
        return bool(await hass.config_entries.async_reload(entry.entry_id))
    except Exception:
        return False


def without_relay_activation(data: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the entry data without the activation code and request id."""
    cleaned = dict(data)
    cleaned.pop(CONF_RELAY_ACTIVATION_CODE, None)
    cleaned.pop(CONF_RELAY_ACTIVATION_REQUEST_ID, None)
    return cleaned


@dataclass(frozen=True)
class RelayMigration:
    """The migrated options, plus what was found in ``hub_config.json``."""

    options: dict[str, Any]
    base_url: str | None
    legacy_present: bool
    legacy_valid: bool


def migrate_relay_options(
    options: Mapping[str, Any], legacy_value: object
) -> RelayMigration:
    """Move a relay URL kept in ``hub_config.json`` into the entry options.

    A valid option already set wins over the legacy value, and an invalid
    URL is dropped from the options rather than kept.
    """
    current = normalize_relay_base_url(options.get(CONF_PUSH_RELAY_URL))
    legacy = normalize_relay_base_url(legacy_value)
    base_url = current or legacy
    migrated = dict(options)
    if base_url is None:
        migrated.pop(CONF_PUSH_RELAY_URL, None)
    else:
        migrated[CONF_PUSH_RELAY_URL] = base_url
    return RelayMigration(
        options=migrated,
        base_url=base_url,
        legacy_present=legacy_value is not None,
        legacy_valid=legacy is not None,
    )
