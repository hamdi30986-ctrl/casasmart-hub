"""CasaSmart runtime component."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from typing import Any, Callable

try:
    from .auth_tokens import ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER
    from .throttle import FailureThrottle
except ImportError:
    from auth_tokens import ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER
    from throttle import FailureThrottle

_LOGGER = logging.getLogger(__name__)


EXPIRY_CHOICES: dict[str, float] = {
    "1d": 24 * 3600.0,
    "1w": 7 * 24 * 3600.0,
    "1m": 30 * 24 * 3600.0,
}
DEFAULT_EXPIRY = "1d"



ISSUABLE_ROLES = (ROLE_SUB_ADMIN, ROLE_USER)





CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 8

BOOTSTRAP_CODE_ID = "bootstrap-admin"





CODE_CLASS_BOOTSTRAP = "bootstrap"
CODE_CLASS_MEMBER = "member"







THROTTLE_PURPOSE_LAN = "lan"
THROTTLE_PURPOSE_REMOTE = "remote"


def _throttle_key(source_key: str, remote_source: bool) -> str:
    """CasaSmart runtime component."""
    purpose = THROTTLE_PURPOSE_REMOTE if remote_source else THROTTLE_PURPOSE_LAN
    return f"{purpose}:{source_key}"


class PairingError(Exception):
    """CasaSmart runtime component."""


class CodeInvalidError(PairingError):
    """CasaSmart runtime component."""


class HubAlreadyClaimedError(PairingError):
    """CasaSmart runtime component."""


class LanOnlyCodeError(PairingError):
    """CasaSmart runtime component."""


def normalize_code(code: str) -> str:
    """CasaSmart runtime component."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(code: str) -> str:
    """CasaSmart runtime component."""
    return hashlib.sha256(normalize_code(code).encode("ascii")).hexdigest()


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _code_class(code_id: str, record: dict[str, Any]) -> str:
    """CasaSmart runtime component."""
    if (
        record.get("code_class") == CODE_CLASS_BOOTSTRAP
        or code_id == BOOTSTRAP_CODE_ID
        or record.get("role") == ROLE_ADMIN
    ):
        return CODE_CLASS_BOOTSTRAP
    return CODE_CLASS_MEMBER


class PairingManager:
    """CasaSmart runtime component."""

    def __init__(
        self,
        codes_table: Any,
        admin_exists: Callable[[], bool],
        throttle: FailureThrottle | None = None,
    ) -> None:
        self._codes = codes_table
        self._admin_exists = admin_exists
        self.throttle = throttle or FailureThrottle("pairing")


        self._lock = threading.Lock()



    def generate_code(
        self,
        role: str,
        rooms: list[str] | None = None,
        expires_in: str = DEFAULT_EXPIRY,
        member_id: str | None = None,
    ) -> dict[str, Any]:
        """CasaSmart runtime component."""
        if member_id is not None and (
            not isinstance(member_id, str) or not member_id
        ):
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

        if rooms is not None and role != ROLE_USER:
            raise PairingError("Room scope only applies to the user role")
        ttl = EXPIRY_CHOICES.get(expires_in)
        if ttl is None:
            raise PairingError(
                f"expires_in must be one of {', '.join(EXPIRY_CHOICES)}"
            )

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
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
        with self._lock:
            if code_id not in self._codes:
                return False
            del self._codes[code_id]
        _LOGGER.info("Pairing code %s revoked", code_id)
        return True

    def clear_all_codes(self) -> int:
        """CasaSmart runtime component."""
        with self._lock:
            count = len(self._codes)
            for code_id in list(self._codes):
                del self._codes[code_id]
        if count:
            _LOGGER.info(
                "Wiped all %d pairing code(s) — pairing factory reset", count
            )
        return count



    def authorize_known_device(
        self,
        code: str,
        source_key: str,
        remote_source: bool = False,
        *,
        allowed_hashes: tuple[str, ...] = (),
    ) -> None:
        """CasaSmart runtime component."""
        throttle_key = _throttle_key(source_key, remote_source)
        self.throttle.check(throttle_key)
        if not isinstance(code, str) or not code.strip():
            self.throttle.record_failure(throttle_key)
            raise CodeInvalidError("Invalid pairing code")
        code_hash = hash_code(code)

        with self._lock:
            self._purge_expired()
            known = any(
                record["code_hash"] == code_hash
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
        """CasaSmart runtime component."""
        throttle_key = _throttle_key(source_key, remote_source)
        self.throttle.check(throttle_key)
        if not isinstance(code, str) or not code.strip():
            self.throttle.record_failure(throttle_key)
            raise CodeInvalidError("Invalid pairing code")
        code_hash = hash_code(code)

        with self._lock:
            self._purge_expired()
            match = next(
                (
                    (code_id, record)
                    for code_id, record in self._codes.items()
                    if record["code_hash"] == code_hash
                ),
                None,
            )
            if match is None:
                self.throttle.record_failure(throttle_key)
                raise CodeInvalidError("Invalid pairing code")
            code_id, record = match
            code_class = _code_class(code_id, record)





            if remote_source and code_class != CODE_CLASS_MEMBER:
                raise LanOnlyCodeError(
                    "This pairing code can only be redeemed on the hub's own network"
                )


            if code_id == BOOTSTRAP_CODE_ID and self._admin_exists():




                raise HubAlreadyClaimedError("This hub is already paired")
            del self._codes[code_id]

        self.throttle.clear(throttle_key)
        _LOGGER.info(
            "Pairing code %s redeemed (role=%s, class=%s%s)",
            code_id,
            record["role"],
            code_class,
            ", remote" if remote_source else "",
        )




        return {
            "role": record["role"],
            "rooms": record.get("rooms"),
            "code_id": code_id,
            "member_id": record.get("member_id"),
            "code_class": code_class,
        }



    def ensure_bootstrap_code(self) -> str | None:
        """CasaSmart runtime component."""
        with self._lock:
            if self._admin_exists():

                if BOOTSTRAP_CODE_ID in self._codes:
                    del self._codes[BOOTSTRAP_CODE_ID]
                return None
            if BOOTSTRAP_CODE_ID in self._codes:
                return None
            code = _new_code()
            self._codes[BOOTSTRAP_CODE_ID] = {
                "code_hash": hash_code(code),
                "role": ROLE_ADMIN,
                "rooms": None,
                "created_at": time.time(),
                "expires_at": None,
                "code_class": CODE_CLASS_BOOTSTRAP,
            }
        _LOGGER.info("Bootstrap admin pairing code generated")
        return code

    def install_bootstrap_hash(self, code_hash: str) -> None:
        """CasaSmart runtime component."""
        with self._lock:
            if self._admin_exists():
                if BOOTSTRAP_CODE_ID in self._codes:
                    del self._codes[BOOTSTRAP_CODE_ID]
                return
            self._codes[BOOTSTRAP_CODE_ID] = {
                "code_hash": code_hash,
                "role": ROLE_ADMIN,
                "rooms": None,
                "created_at": time.time(),
                "expires_at": None,
                "code_class": CODE_CLASS_BOOTSTRAP,
            }



    def _purge_expired(self) -> None:
        """CasaSmart runtime component."""
        now = time.time()
        for code_id in [
            cid
            for cid, record in self._codes.items()
            if record.get("expires_at") and record["expires_at"] <= now
        ]:
            del self._codes[code_id]
