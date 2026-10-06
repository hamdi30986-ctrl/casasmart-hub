"""Push-token storage for B8 — encrypted push notifications.

Each paired device can register one FCM token (keyed by device_id).
The hub stores these so it can fan out encrypted push payloads via
the relay server when alarm/leak/lock events fire.

Thread-safety: delegates to HubStorage's internal lock (KV writes are
serialized). The ``PushTokenStore`` itself is stateless — every call
reads/writes the KV table directly.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

VALID_PLATFORMS = frozenset({"ios", "android"})
MAX_TOKEN_LENGTH = 4096


class PushTokenStore:
    """Thin wrapper around the ``push_tokens`` KV namespace."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def register(
        self,
        device_id: str,
        fcm_token: str,
        platform: str,
    ) -> dict[str, Any]:
        """Store or update a push token for a paired device.

        Upserts by device_id — a token refresh from the same phone
        just overwrites the old token.
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
        """Remove the push token for a device. Returns True if it existed."""
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
