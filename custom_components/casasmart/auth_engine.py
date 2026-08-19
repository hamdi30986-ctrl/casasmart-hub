"""CasaSmart runtime component."""

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
except ImportError:
    import auth_keys
    import auth_tokens
    from auth_tokens import (
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
        VALID_ROLES,
        TokenError,
    )
    from throttle import (
        FailureThrottle,
        ThrottledError,
    )

_LOGGER = logging.getLogger(__name__)


TOKEN_TTL = 45 * 60





WIDGET_TOKEN_TTL = 30 * 24 * 3600

CHALLENGE_TTL = 60.0
MAX_CHALLENGES_PER_DEVICE = 8





MAX_DEVICE_NAME_LENGTH = 64




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
}





WIDGET_SCOPE_PERMISSIONS: frozenset[str] = frozenset(
    {"devices.read", "devices.control"}
)


class AuthError(Exception):
    """CasaSmart runtime component."""


class EnrollError(AuthError):
    """CasaSmart runtime component."""


class AdminExistsError(EnrollError):
    """CasaSmart runtime component."""


class UnknownDeviceError(AuthError):
    """CasaSmart runtime component."""


class ChallengeError(AuthError):
    """CasaSmart runtime component."""


class UserManagementError(AuthError):
    """CasaSmart runtime component."""


class AuthEngine:
    """CasaSmart runtime component."""

    def __init__(self, devices_table: Any, hub_config: Any) -> None:
        self._devices = devices_table
        self._hub_config = hub_config
        self._lock = threading.RLock()

        self._challenges: dict[str, dict[str, Any]] = {}
        self.throttle = FailureThrottle("login")
        self._secret: bytes | None = None




        self._device_cache: dict[str, dict[str, Any]] = {}

    def warm_up(self) -> None:
        """CasaSmart runtime component."""
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



    def _signing_secret(self) -> bytes:
        """CasaSmart runtime component."""
        with self._lock:
            if self._secret is None:
                stored = self._hub_config.get("jwt_secret")
                if not stored:
                    stored = auth_tokens.generate_secret()
                    self._hub_config.set("jwt_secret", stored)
                    _LOGGER.info("Generated new hub JWT signing secret")
                self._secret = bytes.fromhex(stored)
            return self._secret



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
        """CasaSmart runtime component."""
        if not isinstance(name, str) or not name.strip():
            raise EnrollError("Device name is required")



        name = name.strip()[:MAX_DEVICE_NAME_LENGTH].strip()
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
                "enrolled_via": enrolled_via,





                "enrolled_code_hash": code_hash,





                "member_id": member_id or f"mem-{secrets.token_urlsafe(9)}",
            }
            self._device_cache[device_id] = {"role": role, "rooms": rooms, "ver": 1}
        _LOGGER.info("Enrolled device %s (%s, role=%s)", device_id, name, role)
        return device_id

    def ensure_enrolled(
        self,
        device_id: str,
        name: str,
        role: str,
        public_key_pem: str,
        rooms: list[str] | None = None,
    ) -> bool:
        """CasaSmart runtime component."""
        if not isinstance(device_id, str) or not device_id.strip():
            raise EnrollError("device_id is required")
        if not isinstance(name, str) or not name.strip():
            raise EnrollError("Device name is required")


        if role not in (ROLE_SUB_ADMIN, ROLE_USER):
            raise EnrollError("Provisioned role must be sub-admin or user")
        if rooms is not None and (
            not isinstance(rooms, list)
            or any(not isinstance(room, str) or not room for room in rooms)
        ):
            raise EnrollError("rooms must be a list of area ids")
        try:
            canonical_pem = auth_keys.validate_public_key(public_key_pem)
        except auth_keys.KeyError_ as err:
            raise EnrollError(str(err)) from err

        device_id = device_id.strip()
        with self._lock:
            existing = self._devices.get(device_id)
            if (
                existing is not None
                and existing.get("public_key") == canonical_pem
                and existing.get("role") == role
                and existing.get("rooms") == rooms
            ):
                return False

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
        """CasaSmart runtime component."""
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


                "enrolled_via": None,
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
        """CasaSmart runtime component."""
        with self._lock:
            return any(
                entry.get("role") == ROLE_ADMIN
                for entry in self._device_cache.values()
            )



    def list_devices(self) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        with self._lock:
            last_seen = {
                device_id: entry.get("last_seen")
                for device_id, entry in self._device_cache.items()
            }
        return [
            {
                "device_id": device_id,
                "name": record.get("name"),
                "role": record.get("role"),
                "rooms": record.get("rooms"),
                "paired_at": int(record.get("paired_at", 0)),
                "enrolled_via": record.get("enrolled_via"),
                "member_id": record.get("member_id") or device_id,
                "last_seen": last_seen.get(device_id),
            }
            for device_id, record in self._devices.items()
        ]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        """CasaSmart runtime component."""
        record = self._devices.get(device_id)
        if record is None:
            return None
        with self._lock:
            cached = self._device_cache.get(device_id, {})
            last_seen = cached.get("last_seen")
        return {
            "device_id": device_id,
            "name": record.get("name"),
            "role": record.get("role"),
            "rooms": record.get("rooms"),
            "paired_at": int(record.get("paired_at", 0)),
            "enrolled_via": record.get("enrolled_via"),
            "member_id": record.get("member_id") or device_id,
            "last_seen": last_seen,
        }

    def device_for_public_key(self, public_key_pem: str) -> dict[str, Any] | None:
        """CasaSmart runtime component."""
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



                        "enrolled_code_hash": record.get("enrolled_code_hash"),
                    }
        return None

    def member_id_for(self, device_id: str) -> str:
        """CasaSmart runtime component."""
        record = self._devices.get(device_id)
        if record is None:
            return device_id
        return record.get("member_id") or device_id

    def member_device_count(self, member_id: str) -> int:
        """CasaSmart runtime component."""
        return sum(
            1
            for device_id, record in self._devices.items()
            if (record.get("member_id") or device_id) == member_id
        )

    def list_members(self) -> list[dict[str, Any]]:
        """CasaSmart runtime component."""
        members: dict[str, dict[str, Any]] = {}
        for device_id, record in self._devices.items():
            mid = record.get("member_id") or device_id
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
        """CasaSmart runtime component."""
        with self._lock:
            cached = self._device_cache.get(device_id)
            return cached.get("last_seen") if cached else None

    def device_for_token(self, token: str) -> dict[str, Any] | None:
        """CasaSmart runtime component."""
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
        """CasaSmart runtime component."""
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

            if new_rooms is not None and new_role != ROLE_USER:
                raise UserManagementError("Room scope only applies to the user role")

            record["role"] = new_role
            record["rooms"] = new_rooms
            record["ver"] = int(record.get("ver", 1)) + 1
            self._devices[device_id] = record
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
        """CasaSmart runtime component."""
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            if record.get("role") == ROLE_ADMIN:
                raise UserManagementError("The admin account cannot be unpaired")
            member_id = record.get("member_id") or device_id
            del self._devices[device_id]
            self._device_cache.pop(device_id, None)


            self.throttle.clear(device_id)
        _LOGGER.info("Device %s unpaired — all tokens dead", device_id)
        return member_id

    def leave_hub(self, device_id: str) -> str:
        """CasaSmart runtime component."""
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            member_id = record.get("member_id") or device_id
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
        """CasaSmart runtime component."""
        with self._lock:
            wiped = list(self._devices.keys())
            for device_id in wiped:
                del self._devices[device_id]
                self.throttle.clear(device_id)
            self._device_cache.clear()
        if wiped:
            _LOGGER.info(
                "Wiped all %d device(s) — hub reset to unclaimed", len(wiped)
            )
        return wiped



    def create_challenge(self, device_id: str) -> dict[str, Any]:
        """CasaSmart runtime component."""
        self.throttle.check(device_id)
        if device_id not in self._devices:

            self.throttle.record_failure(device_id)
            raise UnknownDeviceError("Unknown device")

        with self._lock:
            self._prune_challenges()
            outstanding = [
                cid
                for cid, challenge in self._challenges.items()
                if challenge["device_id"] == device_id
            ]


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
        """CasaSmart runtime component."""
        self.throttle.check(device_id)

        with self._lock:
            self._prune_challenges()
            challenge = self._challenges.pop(challenge_id, None)

        record = self._devices.get(device_id)
        if (
            record is None
            or challenge is None
            or challenge["device_id"] != device_id
            or not auth_keys.verify_signature(
                record["public_key"], challenge["nonce"], signature_b64
            )
        ):


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



    def validate_token(self, token: str) -> dict[str, Any]:
        """CasaSmart runtime component."""
        claims = auth_tokens.validate_token(self._signing_secret(), token)
        with self._lock:
            cached = self._device_cache.get(claims["sub"])
            if cached is None:




                raise TokenError("Token revoked", code="unenrolled")
            if cached["ver"] != claims.get("ver"):



                raise TokenError("Token revoked", code="token_stale")



            cached["last_seen"] = time.time()





            claims["role"] = cached["role"]
            claims["rooms"] = cached.get("rooms")
        return claims

    def is_owner_device(self, device_id: str) -> bool:
        """CasaSmart runtime component."""
        with self._lock:
            cached = self._device_cache.get(device_id)
            return bool(cached and cached.get("role") == ROLE_ADMIN)

    @staticmethod
    def authorize(claims: dict[str, Any], permission: str) -> bool:
        """CasaSmart runtime component."""
        if (
            claims.get("scope") == auth_tokens.SCOPE_WIDGET
            and permission not in WIDGET_SCOPE_PERMISSIONS
        ):
            return False
        allowed_roles = PERMISSIONS.get(permission)
        if allowed_roles is None:

            _LOGGER.error("authorize() called with unknown permission %r", permission)
            return False
        return claims.get("role") in allowed_roles

    def mint_widget_token(self, device_id: str) -> dict[str, Any]:
        """CasaSmart runtime component."""
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



    def _prune_challenges(self) -> None:
        """CasaSmart runtime component."""
        now = time.monotonic()
        for challenge_id in [
            cid for cid, c in self._challenges.items() if c["expires"] <= now
        ]:
            del self._challenges[challenge_id]
