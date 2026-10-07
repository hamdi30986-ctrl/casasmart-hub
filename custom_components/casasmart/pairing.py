"""Pairing codes: the one-time codes a phone redeems to enroll.

Admin-minted member codes carry the role and room scope, are single-use,
expire, and can be redeemed remotely when remote pairing is on. The bootstrap
code (the printed sticker) grants admin, never expires, is LAN-only and works
only while the hub has no admin; its hash is reinstalled from hub_config at
every boot. Codes are stored as SHA-256 hashes and redemption is throttled.
Storage methods block, so call them in the executor.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

try:
    from .auth_tokens import ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER
    from .throttle import FailureThrottle
except ImportError:  # top-level import in the test env (no HA package init)
    from auth_tokens import (  # type: ignore[no-redef]
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
    )
    from throttle import FailureThrottle  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

EXPIRY_CHOICES: dict[str, float] = {
    "1d": 24 * 3600.0,
    "1w": 7 * 24 * 3600.0,
    "1m": 30 * 24 * 3600.0,
}
DEFAULT_EXPIRY = "1d"
# Roles an admin can put on a code. The only admin code is the bootstrap code,
# and the auth engine enforces the single admin either way.
ISSUABLE_ROLES = (ROLE_SUB_ADMIN, ROLE_USER)
# No 0/O or 1/I/L: people read the sticker and the recovery card (recovery
# uses this alphabet and hash_code too). About 2^39 codes, which the throttle
# makes unguessable even for the bootstrap code that never expires.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 8
# Storage key of the hub's single bootstrap code.
BOOTSTRAP_CODE_ID = "bootstrap-admin"
# Redemption policy is per code class. The bootstrap claim stays LAN-only
# (physical possession); member codes can be redeemed remotely if allowed.
CODE_CLASS_BOOTSTRAP = "bootstrap"
CODE_CLASS_MEMBER = "member"
# Throttle keys are "<purpose>:<source>", so a remote lockout can't block a LAN
# claim showing the same source (a tunnel or proxy can make them match).
THROTTLE_PURPOSE_LAN = "lan"
THROTTLE_PURPOSE_REMOTE = "remote"


class PairingError(Exception):
    """Pairing input rejected, such as a bad role, expiry or room list."""


class CodeInvalidError(PairingError):
    """Unknown, expired or used code; callers can't tell which."""


class HubAlreadyClaimedError(PairingError):
    """The owner code is right, but the hub already has its admin.

    Only another phone gets here, since enroll answers a known public key
    earlier. Kept apart from CodeInvalidError so enroll can say the hub is
    already paired.
    """


class LanOnlyCodeError(PairingError):
    """The code is valid, but its class can't be redeemed off the LAN.

    The secret was right and only the location was wrong, so the code is kept
    and no throttle slot is used. Enroll answers with the LAN gate's 403.
    """


def normalize_code(code: str) -> str:
    """Uppercase ASCII letters and digits only; everything else is dropped.

    Non-ASCII goes before upper-casing (a few non-ASCII letters upper-case to
    ASCII ones), so such input fails as a wrong code, throttle included,
    instead of breaking the ASCII hash.
    """
    return "".join(ch for ch in code if ch.isascii() and ch.isalnum()).upper()


def hash_code(code: str) -> str:
    """SHA-256 hex of the normalized code; mint, redeem and hub_config use it."""
    return hashlib.sha256(normalize_code(code).encode("ascii")).hexdigest()


def _new_code() -> str:
    """A fresh random code from CODE_ALPHABET."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _code_class(code_id: str, record: dict[str, Any]) -> str:
    """A stored code's class; any code that grants admin counts as bootstrap.

    Older records have no code_class field, so the fixed bootstrap id decides
    for them. An admin code must never be redeemable remotely.
    """
    if (
        record.get("code_class") == CODE_CLASS_BOOTSTRAP
        or code_id == BOOTSTRAP_CODE_ID
        or record.get("role") == ROLE_ADMIN
    ):
        return CODE_CLASS_BOOTSTRAP
    return CODE_CLASS_MEMBER


def _bootstrap_record(code_hash: str) -> dict[str, Any]:
    """The stored bootstrap code: grants admin, redeemable until an admin exists."""
    return {
        "code_hash": code_hash,
        "role": ROLE_ADMIN,
        "rooms": None,
        "created_at": time.time(),
        "expires_at": None,
        "code_class": CODE_CLASS_BOOTSTRAP,
    }


def _throttle_key(source_key: str, remote_source: bool) -> str:
    """Redeem throttle key: the source, prefixed by its network class."""
    purpose = THROTTLE_PURPOSE_REMOTE if remote_source else THROTTLE_PURPOSE_LAN
    return f"{purpose}:{source_key}"


class PairingManager:
    """Mints, lists, revokes and redeems pairing codes."""

    def __init__(
        self,
        codes_table: Any,
        admin_exists: Callable[[], bool],
        throttle: FailureThrottle | None = None,
    ) -> None:
        self._codes = codes_table
        self._admin_exists = admin_exists
        self.throttle = throttle or FailureThrottle("pairing")
        # Callers run on executor threads and read-modify-write the table.
        self._lock = threading.Lock()

    # -- admin-facing ----------------------------------------------------------

    def generate_code(
        self,
        role: str,
        rooms: list[str] | None = None,
        expires_in: str = DEFAULT_EXPIRY,
        member_id: str | None = None,
    ) -> dict[str, Any]:
        """Mint a single-use member code; the plaintext is returned only here.

        With a member_id the redeeming phone joins that existing member and
        shares their favorites and settings; the caller passes the member's
        own role and rooms.
        """
        if member_id is not None and (not isinstance(member_id, str) or not member_id):
            raise PairingError("member_id must be a non-empty string")
        if role not in ISSUABLE_ROLES:
            raise PairingError(
                f"Pairing role must be one of {', '.join(ISSUABLE_ROLES)}"
            )
        if rooms is not None and (
            not isinstance(rooms, list)
            or any(not isinstance(room, str) or not room for room in rooms)
        ):
            raise PairingError("rooms must be a list of area ids")
        # Only users are room-scoped; sub-admins see every room.
        if rooms is not None and role != ROLE_USER:
            raise PairingError("Room scope only applies to the user role")
        ttl = EXPIRY_CHOICES.get(expires_in) if isinstance(expires_in, str) else None
        if ttl is None:
            raise PairingError(f"expires_in must be one of {', '.join(EXPIRY_CHOICES)}")

        with self._lock:
            self._purge_expired()
            code = _new_code()
            code_id = f"pair-{secrets.token_urlsafe(8)}"
            now = time.time()
            self._codes[code_id] = {
                "code_hash": hash_code(code),
                "role": role,
                "rooms": rooms,
                "member_id": member_id,
                "created_at": now,
                "expires_at": now + ttl,
                "code_class": CODE_CLASS_MEMBER,
            }
        _LOGGER.info(
            "Pairing code %s generated (role=%s, expires_in=%s)",
            code_id,
            role,
            expires_in,
        )
        return {
            "code_id": code_id,
            "code": code,
            "role": role,
            "rooms": rooms,
            "member_id": member_id,
            "expires_at": int(now + ttl),
            "code_class": CODE_CLASS_MEMBER,
        }

    def list_codes(self) -> list[dict[str, Any]]:
        """Active codes, metadata only (the plaintext is never stored)."""
        with self._lock:
            self._purge_expired()
            return [
                {
                    "code_id": code_id,
                    "role": record["role"],
                    "rooms": record.get("rooms"),
                    "created_at": int(record["created_at"]),
                    "expires_at": (
                        int(record["expires_at"]) if record["expires_at"] else None
                    ),
                    "bootstrap": code_id == BOOTSTRAP_CODE_ID,
                    "code_class": _code_class(code_id, record),
                }
                for code_id, record in self._codes.items()
            ]

    def revoke_code(self, code_id: str) -> bool:
        """Delete a code before it's used; True when it existed."""
        with self._lock:
            if code_id not in self._codes:
                return False
            del self._codes[code_id]
        _LOGGER.info("Pairing code %s revoked", code_id)
        return True

    def clear_all_codes(self) -> int:
        """Delete every code, the bootstrap code included; return the count.

        Part of the "Regenerate pairing code" reset, which then mints a new
        bootstrap code with ensure_bootstrap_code.
        """
        with self._lock:
            count = len(self._codes)
            for code_id in list(self._codes):
                del self._codes[code_id]
        if count:
            _LOGGER.info("Wiped all %d pairing code(s); pairing factory reset", count)
        return count

    # -- enrollment gate ---------------------------------------------------------

    def authorize_known_device(
        self,
        code: str,
        source_key: str,
        remote_source: bool = False,
        *,
        allowed_hashes: tuple[str, ...] = (),
    ) -> None:
        """Check the code on an idempotent re-pair, without consuming it.

        Knowing the phone's key isn't enough: with two hubs on one LAN that
        both know the phone, the app takes the first hub that says yes, so the
        other hub's code could pair it here. The code must be one still in the
        table or one of allowed_hashes (the stored bootstrap code, and the code
        this device first redeemed so a retry after a timeout passes).
        Otherwise raises CodeInvalidError, throttled like redeem.
        """
        throttle_key = _throttle_key(source_key, remote_source)
        code_hash = self._hash_attempt(code, throttle_key)

        with self._lock:
            self._purge_expired()
            known = any(
                hmac.compare_digest(record["code_hash"], code_hash)
                for record in self._codes.values()
            )
        if not known:
            known = any(
                hmac.compare_digest(code_hash, allowed)
                for allowed in allowed_hashes
                if allowed
            )
        if not known:
            self.throttle.record_failure(throttle_key)
            raise CodeInvalidError("Invalid pairing code")
        self.throttle.clear(throttle_key)

    def redeem(
        self, code: str, source_key: str, remote_source: bool = False
    ) -> dict[str, Any]:
        """Consume a code and return its grant, or raise.

        source_key is the request's remote address; each failure counts
        against it in the throttle. remote_source is True for an off-LAN
        request, which auth_api lets through only when remote pairing is on:
        member codes redeem normally, and the bootstrap code raises
        LanOnlyCodeError without being consumed. LAN and remote attempts use
        separate throttle buckets, so neither can lock out the other.
        """
        throttle_key = _throttle_key(source_key, remote_source)
        code_hash = self._hash_attempt(code, throttle_key)

        with self._lock:
            self._purge_expired()
            match = next(
                (
                    (code_id, record)
                    for code_id, record in self._codes.items()
                    if hmac.compare_digest(record["code_hash"], code_hash)
                ),
                None,
            )
            if match is None:
                self.throttle.record_failure(throttle_key)
                raise CodeInvalidError("Invalid pairing code")
            code_id, record = match
            code_class = _code_class(code_id, record)
            # Checked before the claim state, so a remote caller never learns
            # whether the hub is claimed. A right code burns no throttle slot.
            if remote_source and code_class != CODE_CLASS_MEMBER:
                raise LanOnlyCodeError(
                    "This pairing code can only be redeemed on the hub's own network"
                )
            # The bootstrap code is dead once the hub has an admin, so a leaked
            # sticker is useless. The same phone never gets here (see enroll).
            if code_id == BOOTSTRAP_CODE_ID and self._admin_exists():
                raise HubAlreadyClaimedError("This hub is already paired")
            del self._codes[code_id]  # spent even if the enroll that follows fails

        self.throttle.clear(throttle_key)
        _LOGGER.info(
            "Pairing code %s redeemed (role=%s, class=%s%s)",
            code_id,
            record["role"],
            code_class,
            ", remote" if remote_source else "",
        )
        # code_id becomes the device's enrolled_via in the users list.
        return {
            "role": record["role"],
            "rooms": record.get("rooms"),
            "code_id": code_id,
            "member_id": record.get("member_id"),
            "code_class": code_class,
        }

    def _hash_attempt(self, code: str, throttle_key: str) -> str:
        """Hash a submitted code; an empty one counts as a failed attempt.

        Raises ThrottledError while throttle_key is locked out.
        """
        self.throttle.check(throttle_key)
        if not isinstance(code, str) or not code.strip():
            self.throttle.record_failure(throttle_key)
            raise CodeInvalidError("Invalid pairing code")
        return hash_code(code)

    # -- bootstrap ---------------------------------------------------------------

    def ensure_bootstrap_code(self) -> str | None:
        """Make sure an unclaimed hub has a bootstrap code; return a new one.

        Returns the plaintext only when the code was minted here, else None.
        Drops the code once an admin is enrolled.
        """
        with self._lock:
            if self._admin_exists():
                if BOOTSTRAP_CODE_ID in self._codes:
                    del self._codes[BOOTSTRAP_CODE_ID]
                return None
            if BOOTSTRAP_CODE_ID in self._codes:
                return None
            code = _new_code()
            self._codes[BOOTSTRAP_CODE_ID] = _bootstrap_record(hash_code(code))
        _LOGGER.info("Bootstrap admin pairing code generated")
        return code

    def install_bootstrap_hash(self, code_hash: str) -> None:
        """Install the permanent bootstrap code from its hash in hub_config.

        Called at every boot so the printed sticker keeps working; a factory
        reset or "Regenerate pairing code" replaces it. Like
        ensure_bootstrap_code, it drops the code once an admin is enrolled.
        """
        with self._lock:
            if self._admin_exists():
                if BOOTSTRAP_CODE_ID in self._codes:
                    del self._codes[BOOTSTRAP_CODE_ID]
                return
            self._codes[BOOTSTRAP_CODE_ID] = _bootstrap_record(code_hash)

    # -- housekeeping --------------------------------------------------------------

    def _purge_expired(self) -> None:
        """Drop expired codes (caller holds the lock)."""
        now = time.time()
        for code_id in [
            cid
            for cid, record in self._codes.items()
            if record.get("expires_at") and record["expires_at"] <= now
        ]:
            del self._codes[code_id]
