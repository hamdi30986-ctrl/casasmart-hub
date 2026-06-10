"""CasaSmart auth engine (Track B — B1.6): keypairs -> JWT + roles.

The brain behind every authenticated call. Replaces the HA bearer-token
stopgap from B1.4/B1.5 on the same endpoints (plan: "Auth Engine —
keypair validation, JWT issuance/refresh, role enforcement on every API
call").

Flow (plan, "Phone Identity"):

1. **Enroll** (once, at pairing): the app's P-256 public key + name +
   role land in the ``auth_devices`` table. Exactly one admin per hub —
   a second admin enrollment is rejected outright (plan decision
   2026-06-09). B1.6 gates enrollment behind HA's bearer auth as the
   provisioning stopgap; B2 swaps that gate for single-use pairing codes
   without touching this engine.
2. **Challenge**: the app asks for a one-time nonce (60s TTL, single
   use, bound to the device id).
3. **Redeem**: the app returns the nonce signed with its private key;
   the hub verifies with the stored public key and mints a short-lived
   JWT (30-60 min band per plan — silent re-auth is just running this
   flow again before expiry, B9 on the app side).
4. **Validate**: every REST request / WS frame check goes through
   ``validate_token`` -> claims (role + room scope) -> ``authorize``.

Brute-force posture (plan: "Every secret-guessing surface is throttled
server-side"): failed redemptions are counted per device id; 5 failures
inside the window = 30-minute lockout. Counters are in-memory — a hub
reboot clears them, which costs an attacker their progress, not us.

No Home Assistant imports — the engine depends only on the dict-like
storage table and config-store contracts (docs/STORAGE.md), so the whole
thing is unit-testable on a temp SQLite file. Storage-touching methods
are synchronous and must be called via executor from the event loop
(same rule as the rest of the storage layer); in-memory state is
lock-protected so executor threads can't race.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

try:
    from . import auth_keys, auth_tokens
    from .auth_tokens import (
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
        VALID_ROLES,
    )
except ImportError:  # top-level import in the test env (no HA package init)
    import auth_keys  # type: ignore[no-redef]
    import auth_tokens  # type: ignore[no-redef]
    from auth_tokens import (  # type: ignore[no-redef]
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
        VALID_ROLES,
    )

_LOGGER = logging.getLogger(__name__)

# Token lifetime — middle of the plan's 30-60 min band.
TOKEN_TTL = 45 * 60
# One-time login nonces: short-lived, few outstanding per device.
CHALLENGE_TTL = 60.0
MAX_CHALLENGES_PER_DEVICE = 8
# Plan: "5 tries before a 30-min wall" on every secret-guessing surface.
THROTTLE_MAX_FAILURES = 5
THROTTLE_LOCKOUT = 30 * 60.0
# Failure counters are keyed by client-supplied device ids — cap the table
# so id-spam can't grow hub memory unbounded (locked entries are kept).
MAX_THROTTLE_ENTRIES = 1000

# What each role may do. Every endpoint names a permission; the engine is
# the only place the role->permission mapping lives (B2 extends the list,
# it never gets duplicated into views).
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "devices.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "devices.control": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "users.manage": (ROLE_ADMIN,),
    "pairing.generate": (ROLE_ADMIN,),
}


class AuthError(Exception):
    """Base for everything the auth engine can refuse."""


class EnrollError(AuthError):
    """Enrollment input rejected (bad key, bad role...)."""


class AdminExistsError(EnrollError):
    """A second admin enrollment was attempted (plan: exactly one admin)."""


class UnknownDeviceError(AuthError):
    """No enrolled device under that id."""


class ChallengeError(AuthError):
    """Challenge missing, expired, already used, or signature invalid."""


class ThrottledError(AuthError):
    """Too many failures — locked out."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Too many failed attempts, retry in {int(retry_after)}s")
        self.retry_after = retry_after


class AuthEngine:
    """Device enrollment, challenge-response login, JWT mint/validate."""

    def __init__(self, devices_table: Any, hub_config: Any) -> None:
        self._devices = devices_table
        self._hub_config = hub_config
        self._lock = threading.RLock()
        # challenge_id -> {device_id, nonce, expires}
        self._challenges: dict[str, dict[str, Any]] = {}
        # device_id -> {failures, locked_until}
        self._throttle: dict[str, dict[str, float]] = {}
        self._secret: bytes | None = None

    def warm_up(self) -> None:
        """Load (or mint) the signing secret and touch the devices table.

        Blocking file/DB I/O — called once via executor at setup so the
        first ``validate_token`` on the event loop is pure HMAC math.
        """
        self._signing_secret()
        len(self._devices)

    # -- signing secret ------------------------------------------------------

    def _signing_secret(self) -> bytes:
        """The hub's JWT secret — generated once, persisted in hub config."""
        with self._lock:
            if self._secret is None:
                stored = self._hub_config.get("jwt_secret")
                if not stored:
                    stored = auth_tokens.generate_secret()
                    self._hub_config.set("jwt_secret", stored)
                    _LOGGER.info("Generated new hub JWT signing secret")
                self._secret = bytes.fromhex(stored)
            return self._secret

    # -- enrollment (storage — call via executor) ------------------------------

    def enroll_device(
        self,
        name: str,
        role: str,
        public_key_pem: str,
        rooms: list[str] | None = None,
    ) -> str:
        """Store a device's identity; returns the new device id."""
        if not isinstance(name, str) or not name.strip():
            raise EnrollError("Device name is required")
        if role not in VALID_ROLES:
            raise EnrollError(f"Role must be one of {', '.join(VALID_ROLES)}")
        if rooms is not None and (
            not isinstance(rooms, list)
            or any(not isinstance(room, str) or not room for room in rooms)
        ):
            raise EnrollError("rooms must be a list of area ids")
        try:
            canonical_pem = auth_keys.validate_public_key(public_key_pem)
        except auth_keys.KeyError_ as err:
            raise EnrollError(str(err)) from err

        # Plan decision 2026-06-09: exactly one admin per hub, the hub
        # rejects any attempt to create a second one.
        if role == ROLE_ADMIN and any(
            record.get("role") == ROLE_ADMIN for _, record in self._devices.items()
        ):
            raise AdminExistsError("This hub already has an admin")

        device_id = f"dev-{secrets.token_urlsafe(12)}"
        self._devices[device_id] = {
            "name": name.strip(),
            "role": role,
            "public_key": canonical_pem,
            "rooms": rooms,
            "paired_at": time.time(),
        }
        _LOGGER.info("Enrolled device %s (%s, role=%s)", device_id, name, role)
        return device_id

    # -- challenge-response login ---------------------------------------------

    def create_challenge(self, device_id: str) -> dict[str, Any]:
        """Issue a one-time nonce for the device to sign."""
        self._check_throttle(device_id)
        if device_id not in self._devices:
            # Counts as a guess: unknown ids must not be a free probe.
            self._record_failure(device_id)
            raise UnknownDeviceError("Unknown device")

        with self._lock:
            self._prune_challenges()
            outstanding = [
                cid
                for cid, challenge in self._challenges.items()
                if challenge["device_id"] == device_id
            ]
            # Cap outstanding nonces; drop the oldest rather than refuse —
            # an app retrying over a flaky link shouldn't lock itself out.
            while len(outstanding) >= MAX_CHALLENGES_PER_DEVICE:
                self._challenges.pop(outstanding.pop(0), None)

            challenge_id = secrets.token_urlsafe(16)
            nonce = secrets.token_urlsafe(32)
            self._challenges[challenge_id] = {
                "device_id": device_id,
                "nonce": nonce,
                "expires": time.monotonic() + CHALLENGE_TTL,
            }
        return {
            "challenge_id": challenge_id,
            "nonce": nonce,
            "expires_in": int(CHALLENGE_TTL),
        }

    def redeem_challenge(
        self, device_id: str, challenge_id: str, signature_b64: str
    ) -> dict[str, Any]:
        """Verify the signed nonce; mint a JWT on success."""
        self._check_throttle(device_id)

        with self._lock:
            self._prune_challenges()
            challenge = self._challenges.pop(challenge_id, None)  # single use

        record = self._devices.get(device_id)
        if (
            record is None
            or challenge is None
            or challenge["device_id"] != device_id
            or not auth_keys.verify_signature(
                record["public_key"], challenge["nonce"], signature_b64
            )
        ):
            # One generic failure path — the error must not reveal WHICH
            # part was wrong (unknown device vs dead nonce vs bad signature).
            self._record_failure(device_id)
            raise ChallengeError("Challenge verification failed")

        self._clear_failures(device_id)
        token = auth_tokens.issue_token(
            self._signing_secret(),
            device_id=device_id,
            role=record["role"],
            rooms=record.get("rooms"),
            ttl=TOKEN_TTL,
        )
        return {
            "token": token,
            "expires_in": TOKEN_TTL,
            "role": record["role"],
            "device_id": device_id,
        }

    # -- validation + authorization (pure CPU — safe on the event loop) --------

    def validate_token(self, token: str) -> dict[str, Any]:
        """Signature + claims check; returns claims or raises TokenError."""
        return auth_tokens.validate_token(self._signing_secret(), token)

    @staticmethod
    def authorize(claims: dict[str, Any], permission: str) -> bool:
        """True when the token's role grants the named permission."""
        allowed_roles = PERMISSIONS.get(permission)
        if allowed_roles is None:
            # Unknown permission = programming error; fail closed, loudly.
            _LOGGER.error("authorize() called with unknown permission %r", permission)
            return False
        return claims.get("role") in allowed_roles

    @staticmethod
    def allowed_rooms(claims: dict[str, Any]) -> list[str] | None:
        """The token's room scope: a list of area ids, or None = all rooms."""
        return claims.get("rooms")

    # -- throttle ---------------------------------------------------------------

    def _check_throttle(self, device_id: str) -> None:
        with self._lock:
            entry = self._throttle.get(device_id)
            if entry is None:
                return
            remaining = entry.get("locked_until", 0.0) - time.monotonic()
            if remaining > 0:
                raise ThrottledError(remaining)

    def _record_failure(self, device_id: str) -> None:
        with self._lock:
            # Attacker-supplied ids land here too — keep the table bounded
            # by dropping unlocked counters first, oldest insertion first.
            if len(self._throttle) >= MAX_THROTTLE_ENTRIES:
                now = time.monotonic()
                for key in [
                    k
                    for k, v in self._throttle.items()
                    if v.get("locked_until", 0.0) <= now
                ][: len(self._throttle) - MAX_THROTTLE_ENTRIES + 1]:
                    del self._throttle[key]
            entry = self._throttle.setdefault(
                device_id, {"failures": 0.0, "locked_until": 0.0}
            )
            entry["failures"] += 1
            if entry["failures"] >= THROTTLE_MAX_FAILURES:
                entry["locked_until"] = time.monotonic() + THROTTLE_LOCKOUT
                entry["failures"] = 0.0
                _LOGGER.warning(
                    "Auth lockout for %r after %d failures (%.0f min)",
                    device_id,
                    THROTTLE_MAX_FAILURES,
                    THROTTLE_LOCKOUT / 60,
                )

    def _clear_failures(self, device_id: str) -> None:
        with self._lock:
            self._throttle.pop(device_id, None)

    # -- housekeeping -------------------------------------------------------------

    def _prune_challenges(self) -> None:
        """Drop expired nonces (caller holds the lock)."""
        now = time.monotonic()
        for challenge_id in [
            cid for cid, c in self._challenges.items() if c["expires"] <= now
        ]:
            del self._challenges[challenge_id]
