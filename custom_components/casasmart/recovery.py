"""Owner recovery codes: the metal-card tier.

Recovery tiers:

- Cloud keychain restore is tier 1 and lives entirely app-side — the
  restored key just logs in. THIS module is tier 2: the laser-engraved
  metal card for when digital recovery is gone (lost phone + no backup
  + new Apple ID).
- The code is **permanent and reusable** — the printed metal card keeps
  working. Redeeming it does NOT consume it, and its hash is persisted in
  hub_config so the same card survives restarts and reinstalls. A factory
  reset rotates it: the reset deletes the hash and a fresh code is minted
  on reload. Security rests on LAN presence + the escalating throttle +
  the card's physical secrecy, not on single-use.
- Redemption **requires LAN presence** (enforced at the API layer, same
  check as pairing) — a photo taken remotely is useless.
- Redemption replaces the hub's single admin: the old admin device is
  unenrolled (its outstanding JWTs die instantly via the ``ver``
  revocation) and the new phone's keypair becomes the admin. Tier 3
  (a factory reset through Home Assistant, on-site or over remote Home
  Assistant access) is the ``casasmart.factory_reset`` HA service — see
  ``__init__.py``.

Code format: 10 characters from an unambiguous alphabet (no 0/O, 1/I/L),
grouped ``XXXXX-XXXXX`` for engraving. ~49 bits — unguessable through
the escalating throttle, and like the bootstrap pairing code it never
expires. Stored SHA-256-hashed (plaintext exists exactly once, at mint;
re-installed from the stored hash on every boot). Redemption input is
normalized (case, dashes, spaces) so reading the card aloud can't fail
on formatting.

No HA imports — storage-table contract only, unit-testable on a temp
SQLite file. Storage-touching methods are synchronous: call via executor.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

try:
    from .throttle import FailureThrottle
except ImportError:  # top-level import in the test env (no HA package init)
    from throttle import FailureThrottle  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# No 0/O, 1/I/L — the card is read by humans, possibly engraved, possibly
# over the phone to the operator. Every character must be unambiguous.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 10
CODE_GROUP = 5  # display as XXXXX-XXXXX
# Storage key for the one recovery code (there is only ever one per hub).
RECOVERY_CODE_ID = "owner-recovery"


class RecoveryError(Exception):
    """Recovery input rejected."""


class CodeInvalidError(RecoveryError):
    """Wrong code, or none armed — deliberately one bucket."""


def _hash_code(code: str) -> str:
    """SHA-256 hex of an already normalized code."""
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _new_code() -> str:
    """A fresh random ``XXXXX-XXXXX`` code from ``CODE_ALPHABET``."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + CODE_GROUP] for i in range(0, CODE_LENGTH, CODE_GROUP))


def normalize_code(code: str) -> str:
    """Canonical form: uppercase ASCII letters and digits only.

    Dashes and spaces are dropped. Non-ASCII is dropped before upper-casing,
    as in pairing, so it fails as an ordinary wrong code instead of crashing
    the ASCII hash.
    """
    return "".join(ch for ch in code if ch.isascii() and ch.isalnum()).upper()


def hash_code(code: str) -> str:
    """SHA-256 hex of the normalized code.

    One hashing path for mint, redeem and the stored permanent-code hash, so
    all three always agree.
    """
    return _hash_code(normalize_code(code))


class RecoveryManager:
    """Mint / redeem the hub's single owner-recovery code."""

    def __init__(
        self,
        codes_table: Any,
        admin_exists: Callable[[], bool],
        throttle: FailureThrottle | None = None,
    ) -> None:
        self._codes = codes_table
        self._admin_exists = admin_exists
        self.throttle = throttle or FailureThrottle("recovery")
        # Storage writes happen on executor threads; arm + redeem both
        # read-modify-write the table, so serialize them.
        self._lock = threading.Lock()

    def ensure_armed(self) -> str | None:
        """Make sure a claimed hub has an active recovery code.

        Returns the plaintext (dashed) code when a NEW one was just
        minted — the ONLY time it exists in plaintext — so the caller can
        surface it for engraving. None when already armed, or when the
        hub has no admin yet; on such a hub an installed code is DROPPED,
        so call this only once an admin exists. A code minted here is not
        written to hub_config: the stored hash, reinstalled at every boot
        by :meth:`install_recovery_hash`, stays the permanent card.
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
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": _hash_code(normalize_code(code)),
                "created_at": time.time(),
            }
        _LOGGER.info("Owner recovery code armed")
        return code

    def install_recovery_hash(self, code_hash: str) -> None:
        """Install the hub's PERMANENT recovery code from a stored hash.

        Like the bootstrap admin code, the recovery code is engraved once and
        must survive restarts and reinstalls, so its hash is persisted in
        hub_config and re-installed here on every boot (a factory reset
        deletes the hash, so a fresh code is minted instead). Idempotent;
        always armed (even on an unclaimed hub) — redeem is inert until an
        admin exists (replace_admin needs one), so the printed card is ready
        the moment the owner claims.
        """
        with self._lock:
            self._codes[RECOVERY_CODE_ID] = {
                "code_hash": code_hash,
                "created_at": time.time(),
            }

    def mint_permanent(self) -> str:
        """Mint and install a fresh permanent recovery code; return it.

        This is the ONLY time the code exists in the clear: the caller saves
        its hash in hub_config and shows it once for engraving. Used at first
        start (and after a factory reset); thereafter the stored hash is
        re-installed via :meth:`install_recovery_hash`.
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
        """Verify the recovery code, or raise.

        ``source_key`` is the request's remote IP — every failure counts
        against it through the escalating throttle. Failures are one
        generic bucket: wrong code and not-armed are indistinguishable.
        A match is NOT consumed — the engraved card stays valid — and
        clears the source's throttle counter.
        """
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
            # PERMANENT: NOT deleted — the engraved card stays valid.
            # The guard is LAN-only presence + the escalating throttle + the
            # card's physical secrecy; replace_admin (the caller) additionally
            # requires an existing admin, so the code is inert on an unclaimed hub.

        self.throttle.clear(source_key)
        _LOGGER.info("Owner recovery code redeemed (permanent — card stays valid)")
