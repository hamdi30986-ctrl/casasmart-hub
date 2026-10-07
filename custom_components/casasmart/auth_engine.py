"""Auth engine: device enrollment, challenge-response login and JWT checks.

Each paired device has its own P-256 key. It logs in by signing a one-time
nonce and gets a short-lived hub-signed JWT (auth_tokens). An in-memory mirror
of every device's role, rooms and auth version lets token checks run on the
event loop, and makes unpairing or editing a device revoke its tokens at once.
Methods that touch storage block, so call them in the executor.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

# ThrottledError is re-exported; callers catch it as auth_engine.ThrottledError.
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
    import auth_keys
    import auth_tokens
    from auth_tokens import (
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
        VALID_ROLES,
        TokenError,
    )
    from throttle import FailureThrottle, ThrottledError  # noqa: F401

_LOGGER = logging.getLogger(__name__)

# Session token lifetime (seconds); the app logs in again when one expires.
TOKEN_TTL = 45 * 60

# Widget tokens can't log in again on their own, so they live long. They still
# die early with the device's next unpair or role/room edit.
WIDGET_TOKEN_TTL = 30 * 24 * 3600

# A login nonce must come back signed within this many seconds.
CHALLENGE_TTL = 60.0
# Outstanding nonces per device; past it the oldest is dropped.
MAX_CHALLENGES_PER_DEVICE = 8

# Names arrive before the device has authenticated, so the stored name is
# capped. Longer names are truncated so that pairing never fails on one.
MAX_DEVICE_NAME_LENGTH = 64

# Permission -> roles that hold it. Each protected view checks one permission
# through auth_api.authenticate_request; an unknown name is refused.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "devices.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "devices.control": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "history.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "energy.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "energy.control": (ROLE_ADMIN,),
    "energy.manage": (ROLE_ADMIN,),
    "users.manage": (ROLE_ADMIN,),
    "pairing.generate": (ROLE_ADMIN,),
    "registry.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "suggestions.manage": (ROLE_ADMIN,),
    "automations.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "alarm.read": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "alarm.arm": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "alarm.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "cameras.view": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "audio.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "audio.control": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "audio.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "installer.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "update.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "update.install": (ROLE_ADMIN,),
    "widget.token": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    # A session's own writes (push registration, leaving the hub, its member's
    # settings, favorites and suggestions). Widget tokens don't get it.
    "session.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
}

# Everything a widget-scoped token may do, whatever its role.
WIDGET_SCOPE_PERMISSIONS: frozenset[str] = frozenset(
    {"devices.read", "devices.control"}
)


class AuthError(Exception):
    """Base for everything the auth engine can refuse."""


class EnrollError(AuthError):
    """Enrollment input rejected, such as a bad key or role."""


class AdminExistsError(EnrollError):
    """A second admin was refused; a hub has one admin."""


class UnknownDeviceError(AuthError):
    """No enrolled device under that id."""


class ChallengeError(AuthError):
    """Challenge missing, expired, already used, or signature invalid."""


class UserManagementError(AuthError):
    """A user-management edit was refused, for example on the admin."""


def _require_name(name: Any) -> None:
    if not isinstance(name, str) or not name.strip():
        raise EnrollError("Device name is required")


def _canonical_key(public_key_pem: Any) -> str:
    """The key as canonical PEM; raises EnrollError when it isn't usable."""
    try:
        return auth_keys.validate_public_key(public_key_pem)
    except auth_keys.KeyError_ as err:
        raise EnrollError(str(err)) from err


def _member_of(device_id: str, record: dict[str, Any]) -> str:
    """The device's member id; a record without one is its own member."""
    return record.get("member_id") or device_id


def _public_device(
    device_id: str, record: dict[str, Any], last_seen: float | None
) -> dict[str, Any]:
    """A device record's public fields (no key), as the users list shows them."""
    return {
        "device_id": device_id,
        "name": record.get("name"),
        "role": record.get("role"),
        "rooms": record.get("rooms"),
        "paired_at": int(record.get("paired_at", 0)),
        "enrolled_via": record.get("enrolled_via"),
        "member_id": _member_of(device_id, record),
        "last_seen": last_seen,
    }


def _valid_rooms(rooms: Any) -> bool:
    """True for None (all rooms) or a list of non-empty area ids."""
    return rooms is None or (
        isinstance(rooms, list)
        and all(isinstance(room, str) and room for room in rooms)
    )


class AuthEngine:
    """Enrolls devices, runs the login challenge, and mints and checks JWTs."""

    def __init__(self, devices_table: Any, hub_config: Any) -> None:
        self._devices = devices_table
        self._hub_config = hub_config
        self._lock = threading.RLock()
        # challenge_id -> {device_id, nonce, expires}
        self._challenges: dict[str, dict[str, Any]] = {}
        self.throttle = FailureThrottle("login")
        self._secret: bytes | None = None
        # device_id -> {role, rooms, ver, last_seen}. validate_token reads only
        # this, never storage; every write keeps it in sync.
        self._device_cache: dict[str, dict[str, Any]] = {}

    def warm_up(self) -> None:
        """Load the signing secret and device cache (blocking, once at setup).

        Afterwards validate_token needs no I/O and can run on the event loop.
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
        """The hub's JWT secret, generated once and kept in hub config."""
        with self._lock:
            if self._secret is None:
                stored = self._hub_config.get("jwt_secret")
                if not stored:
                    stored = auth_tokens.generate_secret()
                    self._hub_config.set("jwt_secret", stored)
                    _LOGGER.info("Generated new hub JWT signing secret")
                self._secret = bytes.fromhex(stored)
            return self._secret

    # -- enrollment (storage, call via executor) -------------------------------

    def enroll_device(
        self,
        name: str,
        role: str,
        public_key_pem: str,
        rooms: list[str] | None = None,
        enrolled_via: str | None = None,
        member_id: str | None = None,
        code_hash: str | None = None,
    ) -> str:
        """Store a new device and return its random id.

        enrolled_via is the id of the pairing code the device redeemed (None
        when there was none). code_hash is that code's hash, which the
        idempotent re-pair path keeps accepting for this device.
        """
        _require_name(name)
        # The second strip() drops a space the length cap may leave behind.
        name = name.strip()[:MAX_DEVICE_NAME_LENGTH].strip()
        if role not in VALID_ROLES:
            raise EnrollError(f"Role must be one of {', '.join(VALID_ROLES)}")
        if not _valid_rooms(rooms):
            raise EnrollError("rooms must be a list of area ids")
        if rooms is not None and role != ROLE_USER:
            raise EnrollError("Room scope only applies to the user role")
        canonical_pem = _canonical_key(public_key_pem)

        with self._lock:
            if role == ROLE_ADMIN and self.has_admin():
                raise AdminExistsError("This hub already has an admin")

            device_id = f"dev-{secrets.token_urlsafe(12)}"
            self._devices[device_id] = {
                "name": name,
                "role": role,
                "public_key": canonical_pem,
                "rooms": rooms,
                "ver": 1,
                "paired_at": time.time(),
                "enrolled_via": enrolled_via,
                # The re-pair path keeps accepting this code, so a retry after a
                # timeout still works. Older records lack the field.
                "enrolled_code_hash": code_hash,
                # The person this device belongs to: an "add a device" code
                # names an existing member, otherwise the device starts one.
                "member_id": member_id or f"mem-{secrets.token_urlsafe(9)}",
            }
            self._device_cache[device_id] = {"role": role, "rooms": rooms, "ver": 1}
        _LOGGER.info("Enrolled device %s (%s, role=%s)", device_id, name, role)
        return device_id

    @staticmethod
    def check_enrollment(name: Any, public_key_pem: Any) -> None:
        """Raise EnrollError when the name or public key can't be enrolled.

        Lets the enroll view refuse a bad request before it spends a pairing
        code.
        """
        _require_name(name)
        _canonical_key(public_key_pem)

    def ensure_enrolled(
        self,
        device_id: str,
        name: str,
        role: str,
        public_key_pem: str,
        rooms: list[str] | None = None,
    ) -> bool:
        """Enroll or update a sub-admin or user under a caller-chosen id.

        For the developer manifest (dev_enroll), whose ids must survive a
        factory reset. Returns False when the id is already stored with the
        same key, role and rooms, so it is safe to call on every boot.
        Otherwise writes the record, bumping ver to revoke older tokens, and
        updates the cache so the device can log in right away. Never creates an
        admin, so a manifest can't get around the single-admin rule.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise EnrollError("device_id is required")
        _require_name(name)
        if role not in (ROLE_SUB_ADMIN, ROLE_USER):
            raise EnrollError("Provisioned role must be sub-admin or user")
        if not _valid_rooms(rooms):
            raise EnrollError("rooms must be a list of area ids")
        if rooms is not None and role != ROLE_USER:
            raise EnrollError("Room scope only applies to the user role")
        canonical_pem = _canonical_key(public_key_pem)

        device_id = device_id.strip()
        with self._lock:
            existing = self._devices.get(device_id)
            if (
                existing is not None
                and existing.get("public_key") == canonical_pem
                and existing.get("role") == role
                and existing.get("rooms") == rooms
            ):
                return False  # already up to date

            if existing is not None:
                ver = int(existing.get("ver", 1)) + 1
                paired_at = existing.get("paired_at") or time.time()
            else:
                ver = 1
                paired_at = time.time()
            self._devices[device_id] = {
                "name": name.strip(),
                "role": role,
                "public_key": canonical_pem,
                "rooms": rooms,
                "ver": ver,
                "paired_at": paired_at,
                "enrolled_via": None,
            }
            self._device_cache[device_id] = {
                "role": role,
                "rooms": rooms,
                "ver": ver,
            }
        _LOGGER.info(
            "Provisioned device %s (%s, role=%s, ver=%d)",
            device_id,
            name.strip(),
            role,
            ver,
        )
        return True

    def replace_admin(self, name: str, public_key_pem: str) -> str:
        """Swap the hub's admin for a new device (owner recovery); return its id.

        The caller has proven ownership with the recovery code. Inputs are
        checked first and the swap is one transaction, so neither a bad key
        nor a failed write can leave the hub without an admin; the old
        admin's tokens die with its cache entry. The new device keeps the old
        admin's member id, so the owner's favorites and settings carry over
        (no other device can join the admin's member).
        """
        _require_name(name)
        canonical_pem = _canonical_key(public_key_pem)

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
                # An unclaimed hub is claimed with the bootstrap code instead.
                raise EnrollError("This hub has no admin to recover")

            old_record = self._devices.get(old_admin_id) or {}
            member_id = _member_of(old_admin_id, old_record)
            device_id = f"dev-{secrets.token_urlsafe(12)}"
            with self._devices.transaction():
                del self._devices[old_admin_id]
                self._devices[device_id] = {
                    "name": name.strip(),
                    "role": ROLE_ADMIN,
                    "public_key": canonical_pem,
                    "rooms": None,
                    "ver": 1,
                    "paired_at": time.time(),
                    # No pairing code was redeemed; kept for the enroll record shape.
                    "enrolled_via": None,
                    "member_id": member_id,
                }
            self._device_cache.pop(old_admin_id, None)
            self.throttle.clear(old_admin_id)
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
        """True once the hub's admin is enrolled (a cheap cache read)."""
        with self._lock:
            return any(
                entry.get("role") == ROLE_ADMIN for entry in self._device_cache.values()
            )

    # -- user management (storage, call via executor) ----------------------------

    def list_devices(self) -> list[dict[str, Any]]:
        """Every enrolled device, public fields only.

        last_seen is kept in memory: None until the device makes an
        authenticated call after the latest restart.
        """
        with self._lock:
            last_seen = {
                device_id: entry.get("last_seen")
                for device_id, entry in self._device_cache.items()
            }
        return [
            _public_device(device_id, record, last_seen.get(device_id))
            for device_id, record in self._devices.items()
        ]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Public fields for one enrolled device, or None."""
        record = self._devices.get(device_id)
        if record is None:
            return None
        with self._lock:
            cached = self._device_cache.get(device_id, {})
            last_seen = cached.get("last_seen")
        return _public_device(device_id, record, last_seen)

    def device_for_public_key(self, public_key_pem: str) -> dict[str, Any] | None:
        """The enrolled device with this public key, or None.

        Lets enroll answer a phone that re-runs onboarding with its existing
        identity. The id grants nothing: logging in still needs the private
        key.
        """
        try:
            canonical_pem = auth_keys.validate_public_key(public_key_pem)
        except auth_keys.KeyError_:
            return None
        with self._lock:
            for device_id, record in self._devices.items():
                if record.get("public_key") == canonical_pem:
                    return {
                        "device_id": device_id,
                        "role": record.get("role"),
                        "rooms": record.get("rooms"),
                        # Only for the enroll view, which drops it from replies.
                        "enrolled_code_hash": record.get("enrolled_code_hash"),
                    }
        return None

    def member_id_for(self, device_id: str) -> str:
        """The member (person) this device belongs to.

        Favorites and user settings are keyed by it, so they follow the person
        across devices. A record without a member_id (older records, dev
        manifest devices) is its own member, keyed by its device id.
        """
        record = self._devices.get(device_id)
        if record is None:
            return device_id
        return _member_of(device_id, record)

    def member_device_count(self, member_id: str) -> int:
        """How many enrolled devices belong to member_id.

        Callers delete a member's personal data only when this reaches zero.
        """
        return sum(
            1
            for device_id, record in self._devices.items()
            if _member_of(device_id, record) == member_id
        )

    def list_members(self) -> list[dict[str, Any]]:
        """Distinct members with a device count, for the "add a device" picker.

        Name, role and rooms come from the member's most recently paired device.
        """
        members: dict[str, dict[str, Any]] = {}
        for device_id, record in self._devices.items():
            mid = _member_of(device_id, record)
            paired_at = int(record.get("paired_at", 0))
            entry = members.get(mid)
            if entry is None:
                members[mid] = {
                    "member_id": mid,
                    "name": record.get("name"),
                    "role": record.get("role"),
                    "rooms": record.get("rooms"),
                    "device_count": 1,
                    "_paired_at": paired_at,
                }
            else:
                entry["device_count"] += 1
                if paired_at >= entry["_paired_at"]:
                    entry.update(
                        name=record.get("name"),
                        role=record.get("role"),
                        rooms=record.get("rooms"),
                        _paired_at=paired_at,
                    )
        for entry in members.values():
            entry.pop("_paired_at", None)
        return list(members.values())

    def last_seen(self, device_id: str) -> float | None:
        """When the device last presented a valid token; None since a restart.

        An in-memory read, cheap enough for the user sensors on the event loop.
        """
        with self._lock:
            cached = self._device_cache.get(device_id)
            return cached.get("last_seen") if cached else None

    def device_version(self, device_id: str) -> int | None:
        """The device's auth version, or None when it isn't enrolled.

        An in-memory read, safe on the event loop. Every edit bumps it.
        """
        with self._lock:
            cached = self._device_cache.get(device_id)
            return cached["ver"] if cached else None

    def device_for_token(self, token: str) -> dict[str, Any] | None:
        """Public info for the token's device if it is still enrolled, else None.

        Checks the signature but not expiry or ver (see
        auth_tokens.unverified_subject): /auth/whoami asks whether the device
        is still paired, whether or not the token is fresh.
        """
        device_id = auth_tokens.unverified_subject(self._signing_secret(), token)
        if device_id is None:
            return None
        return self.get_device(device_id)

    def update_device(
        self,
        device_id: str,
        role: str | None = None,
        rooms: list[str] | object | None = ...,
    ) -> dict[str, Any]:
        """Change a device's role and/or room scope; its outstanding JWTs die.

        The admin can't be edited here, and the highest role granted is
        sub-admin. rooms=... (the default) leaves the scope unchanged; None
        clears it. Only users can be room-scoped.
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
            if not _valid_rooms(new_rooms):
                raise UserManagementError("rooms must be a list of area ids")
            # Only users are room-scoped; sub-admins see every room.
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

    def delete_device(self, device_id: str) -> str:
        """Unpair a device, killing its tokens; return its member_id.

        The caller uses the member_id to delete the person's favorites and
        settings when this was their last device. The admin can't be removed
        here; it leaves through leave_hub, owner recovery or a factory reset.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            if record.get("role") == ROLE_ADMIN:
                raise UserManagementError("The admin account cannot be unpaired")
            member_id = _member_of(device_id, record)
            del self._devices[device_id]
            self._device_cache.pop(device_id, None)
            # A re-paired phone shouldn't inherit an old lockout.
            self.throttle.clear(device_id)
        _LOGGER.info("Device %s unpaired — all tokens dead", device_id)
        return member_id

    def leave_hub(self, device_id: str) -> str:
        """Unpair a device at its own request, the admin included.

        The only path past delete_device's admin guard, which stops others
        evicting the admin; here the caller proved it holds this device's key.
        It lets "Remove Hub" on the owner's phone hand the hub back, to be
        claimed again with the sticker code. Returns the member_id, as
        delete_device does.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            member_id = _member_of(device_id, record)
            del self._devices[device_id]
            self._device_cache.pop(device_id, None)
            self.throttle.clear(device_id)
        _LOGGER.info(
            "Device %s left the hub at its own request (role=%s) — all tokens dead",
            device_id,
            record.get("role"),
        )
        return member_id

    def wipe_all_devices(self) -> list[str]:
        """Unpair every device, the admin included; return the wiped ids.

        Used by the "Regenerate pairing code" reset: the hub becomes unclaimed
        so a new bootstrap code can be minted, every token dies, and each
        device's login throttle is cleared so a re-paired phone starts clean.
        """
        with self._lock:
            wiped = list(self._devices.keys())
            for device_id in wiped:
                del self._devices[device_id]
                self.throttle.clear(device_id)
            self._device_cache.clear()
        if wiped:
            _LOGGER.info("Wiped all %d device(s) — hub reset to unclaimed", len(wiped))
        return wiped

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
            # Drop the oldest nonce instead of refusing, so an app retrying
            # over a flaky link can't lock itself out.
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
            # One generic failure, so the caller can't tell which check failed.
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

    # -- validation and authorization (no I/O, safe on the event loop) --------

    def validate_token(self, token: str) -> dict[str, Any]:
        """Check the token's signature, claims and revocation; return its claims.

        The device must still be enrolled with the token's auth version, so an
        unpair or edit kills outstanding tokens on their next use. Raises
        TokenError. No I/O, so it is safe on the event loop.
        """
        claims = auth_tokens.validate_token(self._signing_secret(), token)
        with self._lock:
            cached = self._device_cache.get(claims["sub"])
            if cached is None:
                # The cache mirrors every enrolled device, so a miss means it
                # is gone: the one case where the app should pair again.
                raise TokenError("Token revoked", code="unenrolled")
            if cached["ver"] != claims.get("ver"):
                # Edited since the token was minted. Logging in again fixes
                # it, so the app must not re-pair.
                raise TokenError("Token revoked", code="token_stale")
            # For the per-user sensors; kept in memory, so no write per request.
            cached["last_seen"] = time.time()
            # authorize() uses the stored role and rooms. The ver check means
            # they match the claims; this keeps authorize() tied to the record.
            claims["role"] = cached["role"]
            claims["rooms"] = cached.get("rooms")
        return claims

    def is_owner_device(self, device_id: str) -> bool:
        """True when device_id is the admin (owner), who gets owner-only pushes."""
        with self._lock:
            cached = self._device_cache.get(device_id)
            return bool(cached and cached.get("role") == ROLE_ADMIN)

    @staticmethod
    def authorize(claims: dict[str, Any], permission: str) -> bool:
        """True when the claims' role holds the permission.

        claims must come from validate_token, which sets the role and rooms
        from the stored device record. A widget-scoped token is first limited
        to WIDGET_SCOPE_PERMISSIONS, so an admin's widget token can't reach
        admin endpoints.
        """
        if (
            claims.get("scope") == auth_tokens.SCOPE_WIDGET
            and permission not in WIDGET_SCOPE_PERMISSIONS
        ):
            return False
        allowed_roles = PERMISSIONS.get(permission)
        if allowed_roles is None:
            # A programming error: refuse and log it.
            _LOGGER.error("authorize() called with unknown permission %r", permission)
            return False
        return claims.get("role") in allowed_roles

    def mint_widget_token(self, device_id: str) -> dict[str, Any]:
        """Mint the long-lived widget token for an enrolled device.

        Role, rooms and ver come from the device's current record, so the
        token reflects the latest edit and dies with the next one. Raises
        UnknownDeviceError when the device is gone.
        """
        with self._lock:
            cached = self._device_cache.get(device_id)
        if cached is None:
            raise UnknownDeviceError(f"Unknown device {device_id!r}")
        token = auth_tokens.issue_token(
            self._signing_secret(),
            device_id=device_id,
            role=cached["role"],
            rooms=cached.get("rooms"),
            ttl=WIDGET_TOKEN_TTL,
            ver=cached["ver"],
            scope=auth_tokens.SCOPE_WIDGET,
        )
        return {
            "token": token,
            "expires_in": WIDGET_TOKEN_TTL,
            "scope": auth_tokens.SCOPE_WIDGET,
        }

    # -- housekeeping -------------------------------------------------------------

    def _prune_challenges(self) -> None:
        """Drop expired nonces (caller holds the lock)."""
        now = time.monotonic()
        for challenge_id in [
            cid for cid, c in self._challenges.items() if c["expires"] <= now
        ]:
            del self._challenges[challenge_id]
