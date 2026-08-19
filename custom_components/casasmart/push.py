"""CasaSmart runtime component."""

from __future__ import annotations

import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

VALID_PLATFORMS = frozenset({"ios", "android"})
MAX_TOKEN_LENGTH = 4096


class PushTokenStore:
    """CasaSmart runtime component."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def register(
        self,
        device_id: str,
        fcm_token: str,
        platform: str,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        try:
            del self._table[device_id]
            _LOGGER.info("Push token removed for device %s", device_id)
            return True
        except KeyError:
            return False

    def get_all_tokens(self) -> dict[str, dict[str, Any]]:
        """CasaSmart runtime component."""
        return dict(self._table.items())

    def get_token(self, device_id: str) -> dict[str, Any] | None:
        """CasaSmart runtime component."""
        try:
            return self._table[device_id]
        except KeyError:
            return None
