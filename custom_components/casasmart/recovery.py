"""CasaSmart runtime component."""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from typing import Any, Callable

try:
    from .throttle import FailureThrottle
except ImportError:
    from throttle import FailureThrottle

_LOGGER = logging.getLogger(__name__)



CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 10
CODE_GROUP = 5

RECOVERY_CODE_ID = "owner-recovery"


class RecoveryError(Exception):
    """CasaSmart runtime component."""


class CodeInvalidError(RecoveryError):
    """CasaSmart runtime component."""


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(
        raw[i : i + CODE_GROUP] for i in range(0, CODE_LENGTH, CODE_GROUP)
    )


def normalize_code(code: str) -> str:
    """CasaSmart runtime component."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(code: str) -> str:
    """CasaSmart runtime component."""
    return _hash_code(normalize_code(code))


class RecoveryManager:
    """CasaSmart runtime component."""

    def __init__(
        self,
        codes_table: Any,
        admin_exists: Callable[[], bool],
        throttle: FailureThrottle | None = None,
    ) -> None:
        self._codes = codes_table
        self._admin_exists = admin_exists
        self.throttle = throttle or FailureThrottle("recovery")


        self._lock = threading.Lock()

    def ensure_armed(self) -> str | None:
        """CasaSmart runtime component."""
        with self._lock:
            if not self._admin_exists():
                if RECOVERY_CODE_ID in self._codes:
                    del self._codes[RECOVERY_CODE_ID]
                    _LOGGER.info("Stale recovery code dropped (hub unclaimed)")
                return None
            if RECOVERY_CODE_ID in self._codes:
                return None
            code = _new_code()
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": _hash_code(normalize_code(code)),
                "created_at": time.time(),
            }
        _LOGGER.info("Owner recovery code armed")
        return code

    def install_recovery_hash(self, code_hash: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": code_hash,
                "created_at": time.time(),
            }

    def mint_permanent(self) -> str:
        """CasaSmart runtime component."""
        code = _new_code()
        with self._lock:
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": hash_code(code),
                "created_at": time.time(),
            }
        _LOGGER.info("Permanent owner recovery code minted")
        return code

    def is_armed(self) -> bool:
        """CasaSmart runtime component."""
        with self._lock:
            return RECOVERY_CODE_ID in self._codes

    def redeem(self, code: str, source_key: str) -> None:
        """CasaSmart runtime component."""
        self.throttle.check(source_key)
        if not isinstance(code, str) or not code.strip():
            self.throttle.record_failure(source_key)
            raise CodeInvalidError("Invalid recovery code")
        code_hash = hash_code(code)

        with self._lock:
            record = self._codes.get(RECOVERY_CODE_ID)
            if record is None or record["code_hash"] != code_hash:
                self.throttle.record_failure(source_key)
                raise CodeInvalidError("Invalid recovery code")





        self.throttle.clear(source_key)
        _LOGGER.info("Owner recovery code redeemed (permanent — card stays valid)")
