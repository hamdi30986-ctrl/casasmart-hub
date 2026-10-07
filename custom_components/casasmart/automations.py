"""Id checks and list edits behind automation_api, with no HA imports.

automations.yaml is a list of dicts keyed by "id", as in HA's own config
API. Keeping this logic import-free lets the unit tests run without Home
Assistant.
"""

from __future__ import annotations

import re
from typing import Any

# The app's ids look like "casa_automation_20260303_143022015". The prefix is
# the ownership boundary: the API cannot read or write any other automation.
CASA_AUTOMATION_PREFIX = "casa_automation_"

# The app writes only digits and underscores after the prefix, so this
# refuses nothing it generates.
CASA_AUTOMATION_KEY_RE = re.compile(
    r"^" + re.escape(CASA_AUTOMATION_PREFIX) + r"[A-Za-z0-9_]+$"
)

# The longest automation id the hub keeps an Energy Saving flag for.
MAX_AUTOMATION_KEY_LENGTH = 255

CONF_ID = "id"


def is_casa_automation_key(config_key: Any) -> bool:
    """True when the key has the app's prefix and something after it."""
    return (
        isinstance(config_key, str)
        and config_key.startswith(CASA_AUTOMATION_PREFIX)
        and len(config_key) > len(CASA_AUTOMATION_PREFIX)
    )


def is_valid_casa_automation_key(config_key: Any) -> bool:
    """True for one of our keys that is short enough and uses only [A-Za-z0-9_]."""
    return (
        isinstance(config_key, str)
        and len(config_key) <= MAX_AUTOMATION_KEY_LENGTH
        and bool(CASA_AUTOMATION_KEY_RE.match(config_key))
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

    As in HA's EditAutomationConfigView, the id from the URL comes first and
    is written last, so an "id" in the body cannot override it.
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
