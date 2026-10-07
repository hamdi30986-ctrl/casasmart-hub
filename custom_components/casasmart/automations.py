"""Pure automation-config logic — no HA imports.

The id checks and list surgery behind ``automation_api``'s read-modify-write
of automations.yaml, kept import-free so the unit tests run without a
Home Assistant install (same split as ``history.py`` / ``entity_bridge.py``).

automations.yaml is a YAML list of dicts, each carrying an ``id`` key —
HA's own config API treats ``id`` as the primary key and so do we.
"""

from __future__ import annotations

import re
from typing import Any

# The app's automation ids look like "casa_automation_20260303_143022015"
# (the prefix plus a creation timestamp). The prefix IS the ownership
# boundary: config keys without it are not reachable through the CasaSmart
# API, in either direction.
CASA_AUTOMATION_PREFIX = "casa_automation_"

# A well-formed key is the prefix followed by one or more characters drawn
# ONLY from [A-Za-z0-9_]. The app only ever generates digits and underscores
# after the prefix, so this rejects nothing real. It refuses a hand-crafted
# key carrying dashes, dots, slashes, spaces or other characters that have
# no business in an automations.yaml ``id``.
CASA_AUTOMATION_KEY_RE = re.compile(
    r"^" + re.escape(CASA_AUTOMATION_PREFIX) + r"[A-Za-z0-9_]+$"
)

# The primary-key field of each automations.yaml entry.
CONF_ID = "id"


def is_casa_automation_key(config_key: Any) -> bool:
    """True when the config key is one of the app's own automations.

    Ownership gate only — the ``casa_automation_`` prefix plus a non-empty
    suffix. Charset validity is a separate, stricter check
    (:func:`is_valid_casa_automation_key`).
    """
    return (
        isinstance(config_key, str)
        and config_key.startswith(CASA_AUTOMATION_PREFIX)
        and len(config_key) > len(CASA_AUTOMATION_PREFIX)
    )


def is_valid_casa_automation_key(config_key: Any) -> bool:
    """True when the key is one of ours AND well-formed.

    Stricter than :func:`is_casa_automation_key`: the prefix must be
    followed by letters, digits or underscores only. A key that owns the
    prefix but carries any other character (dash, dot, space, path
    separator, ...) is ours-but-malformed and must be refused, not written
    into automations.yaml.
    """
    return isinstance(config_key, str) and bool(
        CASA_AUTOMATION_KEY_RE.match(config_key)
    )


def get_automation(
    data: list[dict[str, Any]], config_key: str
) -> dict[str, Any] | None:
    """The stored config with this id, or None."""
    for item in data:
        if str(item.get(CONF_ID)) == config_key:
            return item
    return None


def upsert_automation(
    data: list[dict[str, Any]], config_key: str, new_value: dict[str, Any]
) -> None:
    """Create or replace the config with this id, in place.

    Mirrors HA's own EditAutomationConfigView._write_value: the stored
    item is ``{id} + body`` with the URL's key written LAST — a
    client-supplied ``id`` in the body can never overrule the URL.
    """
    updated = {CONF_ID: config_key}
    updated.update(new_value)
    updated[CONF_ID] = config_key
    for index, item in enumerate(data):
        if str(item.get(CONF_ID)) == config_key:
            data[index] = updated
            return
    data.append(updated)


def delete_automation(data: list[dict[str, Any]], config_key: str) -> bool:
    """Remove the config with this id, in place; False if it was not there."""
    for index, item in enumerate(data):
        if str(item.get(CONF_ID)) == config_key:
            del data[index]
            return True
    return False
