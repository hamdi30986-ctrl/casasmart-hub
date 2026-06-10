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
server-side"): failed redemptions are counted per device id through the
shared ``FailureThrottle`` (B2 — 5 failures -> escalating lockout,
1 min -> 5 -> 30 -> 1 hr). Counters are in-memory — a hub reboot clears
them, which costs an attacker their progress, not us.

B2 additions: user management (list / role + room edits / unpair) with
the single-admin invariant enforced on every path, and **instant
revocation** — the engine keeps an in-memory mirror of every device's
{role, rooms, version}; ``validate_token`` checks the token's ``ver``
claim against it, so deleting a device or editing its privileges kills
outstanding JWTs on the very next request (plan: "Delete public key
from hub = instant kill... no logout needed").

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
        TokenError,
    )
    from .throttle import FailureThrottle, ThrottledError
except ImportError:  # top-level import in the test env (no HA package init)
    import auth_keys  # type: ignore[no-redef]
    import auth_tokens  # type: ignore[no-redef]
    from auth_tokens import (  # type: ignore[no-redef]
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
        VALID_ROLES,
        TokenError,
    )
    from throttle import FailureThrottle, ThrottledError  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Token lifetime — middle of the plan's 30-60 min band.
TOKEN_TTL = 45 * 60
# One-time login nonces: short-lived, few outstanding per device.
CHALLENGE_TTL = 60.0
MAX_CHALLENGES_PER_DEVICE = 8

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


class UserManagementError(AuthError):
    """A user-management edit was refused (unknown id, admin protected...)."""


class AuthEngine:
    """Device enrollment, challenge-response login, JWT mint/validate."""

    def __init__(self, devices_table: Any, hub_config: Any) -> None:
        self._devices = devices_table
        self._hub_config = hub_config
        self._lock = threading.RLock()
        # challenge_id -> {device_id, nonce, expires}
        self._challenges: dict[str, dict[str, Any]] = {}
        self.throttle = FailureThrottle("login")
        self._secret: bytes | None = None
        # In-memory mirror of every device's auth-relevant state:
        # device_id -> {role, rooms, ver}. validate_token reads ONLY this
        # (no DB hop on the event loop); enroll/update/delete keep it in
        # sync, which is what makes revocation instant.
        self._device_cache: dict[str, dict[str, Any]] = {}

    def warm_up(self) -> None:
        """Load the signing secret + device cache from storage.

        Blocking file/DB I/O — called once via executor at setup so the
        first ``validate_token`` on the event loop is pure CPU.
        """
        self._signing_secret()
        with self._lock:
            self._device_cache = {
                device_id: {
                    "role": record.get("role"),
                    "rooms": record.get("rooms"),
                    "ver": int(record.get("ver", 1)),
                }
                for device_id, record in self._devices.items()
            }

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

        with self._lock:
            # Plan decision 2026-06-09: exactly one admin per hub, the hub
            # rejects any attempt to create a second one.
            if role == ROLE_ADMIN and self.has_admin():
                raise AdminExistsError("This hub already has an admin")

            device_id = f"dev-{secrets.token_urlsafe(12)}"
            self._devices[device_id] = {
                "name": name.strip(),
                "role": role,
                "public_key": canonical_pem,
                "rooms": rooms,
                "ver": 1,
                "paired_at": time.time(),
            }
            self._device_cache[device_id] = {"role": role, "rooms": rooms, "ver": 1}
        _LOGGER.info("Enrolled device %s (%s, role=%s)", device_id, name, role)
        return device_id

    def replace_admin(self, name: str, public_key_pem: str) -> str:
        """Swap the hub's admin for a new device (B3 owner recovery).

        The ONE sanctioned path around "the admin record is immutable":
        the caller has already proven ownership by redeeming the
        single-use recovery code (LAN-only, throttled). Atomic under the
        lock — the old admin device is unenrolled (its outstanding JWTs
        die instantly via the ``ver`` cache, same as unpair) and the new
        keypair becomes the admin. Inputs are validated BEFORE the old
        admin is touched, so a bad key never leaves the hub adminless.
        """
        if not isinstance(name, str) or not name.strip():
            raise EnrollError("Device name is required")
        try:
            canonical_pem = auth_keys.validate_public_key(public_key_pem)
        except auth_keys.KeyError_ as err:
            raise EnrollError(str(err)) from err

        with self._lock:
            old_admin_id = next(
                (
                    device_id
                    for device_id, entry in self._device_cache.items()
                    if entry.get("role") == ROLE_ADMIN
                ),
                None,
            )
            if old_admin_id is None:
                # Unclaimed hub: recovery has nothing to replace — the
                # bootstrap pairing code is the right door.
                raise EnrollError("This hub has no admin to recover")

            del self._devices[old_admin_id]
            self._device_cache.pop(old_admin_id, None)
            self.throttle.clear(old_admin_id)

            device_id = f"dev-{secrets.token_urlsafe(12)}"
            self._devices[device_id] = {
                "name": name.strip(),
                "role": ROLE_ADMIN,
                "public_key": canonical_pem,
                "rooms": None,
                "ver": 1,
                "paired_at": time.time(),
            }
            self._device_cache[device_id] = {
                "role": ROLE_ADMIN,
                "rooms": None,
                "ver": 1,
            }
        _LOGGER.info(
            "Owner recovery: admin %s replaced by %s (%s) — old admin tokens dead",
            old_admin_id,
            device_id,
            name.strip(),
        )
        return device_id

    def has_admin(self) -> bool:
        """True once the hub's single admin is enrolled (cache read — cheap)."""
        with self._lock:
            return any(
                entry.get("role") == ROLE_ADMIN
                for entry in self._device_cache.values()
            )

    # -- user management (storage — call via executor) ---------------------------

    def list_devices(self) -> list[dict[str, Any]]:
        """Every enrolled device, public fields only (no keys)."""
        return [
            {
                "device_id": device_id,
                "name": record.get("name"),
                "role": record.get("role"),
                "rooms": record.get("rooms"),
                "paired_at": int(record.get("paired_at", 0)),
            }
            for device_id, record in self._devices.items()
        ]

    def update_device(
        self,
        device_id: str,
        role: str | None = None,
        rooms: list[str] | None | object = ...,
    ) -> dict[str, Any]:
        """Edit a device's role and/or room scope; outstanding JWTs die.

        The admin record is immutable here — there is no demotion path
        (plan: factory reset is how an admin changes hands). Promotion
        ceiling is sub-admin: ``role`` may only be sub-admin or user.
        ``rooms=...`` (the sentinel) means "leave unchanged"; an explicit
        None clears the scope. Room scope only applies to the user role.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            if record.get("role") == ROLE_ADMIN:
                raise UserManagementError("The admin account cannot be modified")
            new_role = role if role is not None else record.get("role")
            if new_role not in (ROLE_SUB_ADMIN, ROLE_USER):
                raise UserManagementError("Role must be sub-admin or user")
            new_rooms = record.get("rooms") if rooms is ... else rooms
            if new_rooms is not None and (
                not isinstance(new_rooms, list)
                or any(not isinstance(room, str) or not room for room in new_rooms)
            ):
                raise UserManagementError("rooms must be a list of area ids")
            # Plan: room-scoping is a per-USER toggle; sub-admins see all rooms.
            if new_rooms is not None and new_role != ROLE_USER:
                raise UserManagementError("Room scope only applies to the user role")

            record["role"] = new_role
            record["rooms"] = new_rooms
            record["ver"] = int(record.get("ver", 1)) + 1
            self._devices[device_id] = record  # persist
            self._device_cache[device_id] = {
                "role": new_role,
                "rooms": new_rooms,
                "ver": record["ver"],
            }
        _LOGGER.info(
            "Device %s updated (role=%s, rooms=%s) — outstanding tokens invalidated",
            device_id,
            new_role,
            "all" if new_rooms is None else len(new_rooms),
        )
        return {
            "device_id": device_id,
            "name": record.get("name"),
            "role": new_role,
            "rooms": new_rooms,
            "paired_at": int(record.get("paired_at", 0)),
        }

    def delete_device(self, device_id: str) -> None:
        """Unpair a device — instant kill for all its tokens (plan B2).

        The admin cannot be deleted through the API; factory reset is the
        only way the owner identity leaves the hub.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            if record.get("role") == ROLE_ADMIN:
                raise UserManagementError("The admin account cannot be unpaired")
            del self._devices[device_id]
            self._device_cache.pop(device_id, None)
            # Their login surface resets too — stale lockouts shouldn't
            # follow a re-pair of the same phone.
            self.throttle.clear(device_id)
        _LOGGER.info("Device %s unpaired — all tokens dead", device_id)

    # -- challenge-response login ---------------------------------------------

    def create_challenge(self, device_id: str) -> dict[str, Any]:
        """Issue a one-time nonce for the device to sign."""
        self.throttle.check(device_id)
        if device_id not in self._devices:
            # Counts as a guess: unknown ids must not be a free probe.
            self.throttle.record_failure(device_id)
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
        self.throttle.check(device_id)

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
            self.throttle.record_failure(device_id)
            raise ChallengeError("Challenge verification failed")

        self.throttle.clear(device_id)
        token = auth_tokens.issue_token(
            self._signing_secret(),
            device_id=device_id,
            role=record["role"],
            rooms=record.get("rooms"),
            ttl=TOKEN_TTL,
            ver=int(record.get("ver", 1)),
        )
        return {
            "token": token,
            "expires_in": TOKEN_TTL,
            "role": record["role"],
            "device_id": device_id,
        }

    # -- validation + authorization (pure CPU — safe on the event loop) --------

    def validate_token(self, token: str) -> dict[str, Any]:
        """Signature + claims + revocation check; claims or TokenError.

        Beyond the cryptographic check, the token must point at a device
        that is STILL enrolled with the SAME auth version — unpairing or
        editing a device kills its outstanding JWTs here, on the next
        request (plan B2: "Delete public key from hub = instant kill").
        Pure in-memory work — safe on the event loop.
        """
        claims = auth_tokens.validate_token(self._signing_secret(), token)
        with self._lock:
            cached = self._device_cache.get(claims["sub"])
        if cached is None or cached["ver"] != claims.get("ver"):
            raise TokenError("Token revoked")
        return claims

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

    # -- housekeeping -------------------------------------------------------------

    def _prune_challenges(self) -> None:
        """Drop expired nonces (caller holds the lock)."""
        now = time.monotonic()
        for challenge_id in [
            cid for cid, c in self._challenges.items() if c["expires"] <= now
        ]:
            del self._challenges[challenge_id]
