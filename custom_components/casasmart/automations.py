"""Pure automation-config logic (B16 stage 3c-3) — no HA imports.

The list surgery behind ``automation_api.py``'s read-modify-write on
automations.yaml, kept import-free so the unit tests run without a
Home Assistant install (same split as ``history.py`` / ``entity_bridge.py``).

automations.yaml is a YAML list of dicts, each carrying an ``id`` key —
HA's own config API treats ``id`` as the primary key and so do we.
"""

from __future__ import annotations

from typing import Any

# The app's automation ids look like "casa_automation_20260303_143022015"
# (CasaAutomation.generateId). The prefix IS the ownership boundary:
# config keys without it are not reachable through the CasaSmart API,
# in either direction.
CASA_AUTOMATION_PREFIX = "casa_automation_"

CONF_ID = "id"


def is_casa_automation_key(config_key: Any) -> bool:
    """True when the config key is one of the app's own automations."""
    return (
        isinstance(config_key, str)
        and config_key.startswith(CASA_AUTOMATION_PREFIX)
        and len(config_key) > len(CASA_AUTOMATION_PREFIX)
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
    """Remove the config with this id, in place. False = wasn't there."""
    for index, item in enumerate(data):
        if str(item.get(CONF_ID)) == config_key:
            del data[index]
            return True
    return False
