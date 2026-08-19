"""CasaSmart runtime component."""

from __future__ import annotations

import re
from typing import Any





CASA_AUTOMATION_PREFIX = "casa_automation_"







CASA_AUTOMATION_KEY_RE = re.compile(
    r"^" + re.escape(CASA_AUTOMATION_PREFIX) + r"[A-Za-z0-9_]+$"
)

CONF_ID = "id"


def is_casa_automation_key(config_key: Any) -> bool:
    """CasaSmart runtime component."""
    return (
        isinstance(config_key, str)
        and config_key.startswith(CASA_AUTOMATION_PREFIX)
        and len(config_key) > len(CASA_AUTOMATION_PREFIX)
    )


def is_valid_casa_automation_key(config_key: Any) -> bool:
    """CasaSmart runtime component."""
    return isinstance(config_key, str) and bool(
        CASA_AUTOMATION_KEY_RE.match(config_key)
    )


def get_automation(
    data: list[dict[str, Any]], config_key: str
) -> dict[str, Any] | None:
    """CasaSmart runtime component."""
    for item in data:
        if str(item.get(CONF_ID)) == config_key:
            return item
    return None


def upsert_automation(
    data: list[dict[str, Any]], config_key: str, new_value: dict[str, Any]
) -> None:
    """CasaSmart runtime component."""
    updated = {CONF_ID: config_key}
    updated.update(new_value)
    updated[CONF_ID] = config_key
    for index, item in enumerate(data):
        if str(item.get(CONF_ID)) == config_key:
            data[index] = updated
            return
    data.append(updated)


def delete_automation(data: list[dict[str, Any]], config_key: str) -> bool:
    """CasaSmart runtime component."""
    for index, item in enumerate(data):
        if str(item.get(CONF_ID)) == config_key:
            del data[index]
            return True
    return False
