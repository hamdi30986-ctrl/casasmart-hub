"""Push-token storage: where each paired device wants its notifications.

One FCM token per paired device, keyed by device id. push_api writes them;
push_dispatcher reads them all and drops any the relay reports dead.
Unpairing removes a device's token, and the pairing-code and factory resets
remove them all. The store keeps no state of its own; HubStorage serializes
the writes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# What the push-token endpoint accepts.
VALID_PLATFORMS = frozenset({"ios", "android"})
MAX_TOKEN_LENGTH = 4096


class PushTokenStore:
    """The push_tokens table: one record per device."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def register(
        self,
        device_id: str,
        fcm_token: str,
        platform: str,
    ) -> dict[str, Any]:
        """Store or replace a device's push token.

        Raises ValueError for an unknown platform or an empty, blank or
        oversized token.
        """
        if platform not in VALID_PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(VALID_PLATFORMS)}")
        if not fcm_token or not fcm_token.strip() or len(fcm_token) > MAX_TOKEN_LENGTH:
            raise ValueError("fcm_token is empty, blank, or too long")

        record = {
            "fcm_token": fcm_token,
            "platform": platform,
            "updated_at": time.time(),
        }
        self._table[device_id] = record
        _LOGGER.info("Push token registered for device %s (%s)", device_id, platform)
        return record

    def unregister(self, device_id: str) -> bool:
        """Remove a device's push token; True if it existed."""
        try:
            del self._table[device_id]
            _LOGGER.info("Push token removed for device %s", device_id)
            return True
        except KeyError:
            return False

    def get_all_tokens(self) -> dict[str, dict[str, Any]]:
        """Return {device_id: record} for every registered push token."""
        return dict(self._table.items())

    def get_token(self, device_id: str) -> dict[str, Any] | None:
        """Return the push token record for one device, or None."""
        try:
            return self._table[device_id]
        except KeyError:
            return None
