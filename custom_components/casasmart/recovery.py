"""Owner recovery code: the engraved card that gets the owner back in.

Redeeming the code lets a new phone replace the hub's admin when the old phone
and its key backup are gone. The code is permanent and reusable: its hash is
kept in hub_config so the card survives restarts, and a factory reset rotates
it. Redemption is LAN-only (checked in auth_api) and throttled per source.
Storage methods block, so call them in the executor.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

try:
    from .pairing import CODE_ALPHABET, hash_code
    from .throttle import FailureThrottle
except ImportError:  # top-level import in the test env (no HA package init)
    from pairing import CODE_ALPHABET, hash_code  # type: ignore[no-redef]
    from throttle import FailureThrottle  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

CODE_LENGTH = 10
CODE_GROUP = 5  # characters per dash-separated group
# Storage key of the hub's single recovery code.
RECOVERY_CODE_ID = "owner-recovery"


class RecoveryError(Exception):
    """Recovery input rejected."""


class CodeInvalidError(RecoveryError):
    """Wrong code or no code armed; callers can't tell which."""


def _new_code() -> str:
    """A fresh random code from CODE_ALPHABET, grouped for engraving."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + CODE_GROUP] for i in range(0, CODE_LENGTH, CODE_GROUP))


class RecoveryManager:
    """Mints and redeems the hub's single owner recovery code.

    save_hash stores a code's hash in hub_config, where boot reinstalls the
    permanent card from.
    """

    def __init__(
        self,
        codes_table: Any,
        admin_exists: Callable[[], bool],
        throttle: FailureThrottle | None = None,
        *,
        save_hash: Callable[[str], None],
    ) -> None:
        self._codes = codes_table
        self._admin_exists = admin_exists
        self._save_hash = save_hash
        self.throttle = throttle or FailureThrottle("recovery")
        # Callers run on executor threads and read-modify-write the table.
        self._lock = threading.Lock()

    def ensure_armed(self) -> str | None:
        """Make sure a claimed hub has a recovery code; return a new one.

        Returns the plaintext only when the code was minted here, else None.
        On a hub without an admin it drops any installed code, so call it only
        once an admin exists. A new code's hash is saved first, so the code
        shown is the permanent card; if saving raises, nothing is installed.
        """
        with self._lock:
            if not self._admin_exists():
                if RECOVERY_CODE_ID in self._codes:
                    del self._codes[RECOVERY_CODE_ID]
                    _LOGGER.info("Stale recovery code dropped (hub unclaimed)")
                return None
            if RECOVERY_CODE_ID in self._codes:
                return None
            code = _new_code()
            code_hash = hash_code(code)
            self._save_hash(code_hash)
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": code_hash,
                "created_at": time.time(),
            }
        _LOGGER.info("Owner recovery code armed")
        return code

    def install_recovery_hash(self, code_hash: str) -> None:
        """Install the permanent recovery code from its hash in hub_config.

        Called at every boot, even on an unclaimed hub: redeeming does nothing
        until replace_admin has an admin to replace, and the card then works
        as soon as the owner claims the hub.
        """
        with self._lock:
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": code_hash,
                "created_at": time.time(),
            }

    def mint_permanent(self) -> str:
        """Mint and install a new permanent recovery code; return it.

        The only time the code exists in plaintext: the caller saves its hash
        in hub_config and shows the code once for engraving. Used at first
        start and after a factory reset.
        """
        code = _new_code()
        with self._lock:
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": hash_code(code),
                "created_at": time.time(),
            }
        _LOGGER.info("Permanent owner recovery code minted")
        return code

    def is_armed(self) -> bool:
        """True while a recovery code is installed (redeeming doesn't use it up)."""
        with self._lock:
            return RECOVERY_CODE_ID in self._codes

    def redeem(self, code: str, source_key: str) -> None:
        """Check the recovery code; raise CodeInvalidError when it doesn't match.

        source_key is the request's remote address, and each failure counts
        against it in the throttle. A match leaves the code in place and
        clears the source's counter.
        """
        self.throttle.check(source_key)
        if not isinstance(code, str) or not code.strip():
            self.throttle.record_failure(source_key)
            raise CodeInvalidError("Invalid recovery code")
        code_hash = hash_code(code)

        with self._lock:
            record = self._codes.get(RECOVERY_CODE_ID)
            if record is None or not hmac.compare_digest(
                record["code_hash"], code_hash
            ):
                self.throttle.record_failure(source_key)
                raise CodeInvalidError("Invalid recovery code")
            # Not deleted: the card is permanent. LAN-only access, the throttle
            # and replace_admin's need for an existing admin protect it.

        self.throttle.clear(source_key)
        _LOGGER.info("Owner recovery code redeemed (permanent — card stays valid)")
