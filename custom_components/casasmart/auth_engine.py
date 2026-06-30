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
# Widget token lifetime (B16 3c-3). Long-lived on purpose: a home-screen
# widget can't run the challenge-response login, and the app re-mints a
# fresh one on every widget credential sync, so 30 days is the OUTER
# bound, not the rotation cadence. Revocation doesn't wait for expiry —
# the ``ver`` check kills it the moment the device is edited/unpaired.
WIDGET_TOKEN_TTL = 30 * 24 * 3600
# One-time login nonces: short-lived, few outstanding per device.
CHALLENGE_TTL = 60.0
MAX_CHALLENGES_PER_DEVICE = 8

# Upper bound on a device/member name. Enroll is pre-auth (the pairing code is
# the only gate), so the name is attacker-influenced input — cap it so a single
# enrollment can't store an unbounded blob. Over-length names are truncated, not
# rejected: a long name must never cost the user their one-shot pairing code.
MAX_DEVICE_NAME_LENGTH = 64

# What each role may do. Every endpoint names a permission; the engine is
# the only place the role->permission mapping lives (B2 extends the list,
# it never gets duplicated into views).
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "devices.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "devices.control": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    # B16 3c-3: recorded state history — read-only, same audience as the
    # device list (the energy screen is a user-facing surface).
    "history.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "users.manage": (ROLE_ADMIN,),
    "pairing.generate": (ROLE_ADMIN,),
    # B17: organize the home (floors/rooms/assignments/scenes). Users are
    # read-only — they see the layout through the registry GET, never edit it.
    "registry.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    # B16 3c-3: automation config CRUD (create/edit/delete in
    # automations.yaml). Same trust tier as registry.manage — family
    # members may toggle/trigger (devices.control), never rewrite.
    "automations.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    # B13 alarm: read arm state + history, arm/disarm, configure zones.
    # Plan roles table — admin + sub-admin only; a plain user has NO alarm
    # access (not even read). Split into three verbs so a future read-only
    # tier is a one-line change, not a redesign.
    "alarm.read": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "alarm.arm": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    "alarm.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    # B16 3c-3: camera snapshots + HLS stream minting. Same audience as
    # the device list — cameras are user-facing tiles; room scoping still
    # applies per-entity through ``in_scope`` like everything else.
    "cameras.view": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    # B14 audio: the speaker stack. ``read`` (see the speaker list + athan
    # config) and ``control`` (volume/stop/play/PA broadcast) mirror the
    # device tiers — speakers are a user-facing surface the whole household
    # uses. ``manage`` (enroll/remove speakers, edit athan, set broker/PA
    # creds) is admin+sub-admin only: it reshapes the audio hardware + holds
    # the broker credentials, same trust tier as ``registry.manage``.
    "audio.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "audio.control": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "audio.manage": (ROLE_ADMIN, ROLE_SUB_ADMIN),
    # B16 3c-4b: the installer surface — Zigbee permit-join, raw HA
    # registry reads + entity surgery (rename only), the
    # whitelisted config-flow proxy (Broadlink/EasyIR) and IR blaster
    # commands. Owner-only: these reshape the home's hardware.
    "installer.manage": (ROLE_ADMIN,),
    # B5 self-update. ``read`` (current vs latest version + changelog) is a
    # user-facing surface — every role sees the red dot. ``install`` triggers
    # the hub to self-replace and restart, so it is owner-only, the same trust
    # tier as users.manage / installer.manage.
    "update.read": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
    "update.install": (ROLE_ADMIN,),
    # B16 3c-3 widgets: mint the long-lived, narrow widget token. Every
    # role may mint one FOR ITSELF — the minted token inherits the
    # caller's own role/rooms/ver, so it can never out-privilege the
    # session that requested it.
    "widget.token": (ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER),
}

# What a ``scope: widget`` token may do — and the COMPLETE list of it.
# Read states for tiles, send whitelisted device commands. No history,
# no cameras, no automation CRUD, no pairing/user management, and no
# minting further widget tokens (so a leaked one can't self-renew).
WIDGET_SCOPE_PERMISSIONS: frozenset[str] = frozenset(
    {"devices.read", "devices.control"}
)


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
        enrolled_via: str | None = None,
        member_id: str | None = None,
    ) -> str:
        """Store a device's identity; returns the new device id.

        ``enrolled_via`` records the pairing code id the device redeemed (None
        for paths that don't go through one), retained on the device record for
        potential per-code revocation.
        """
        if not isinstance(name, str) or not name.strip():
            raise EnrollError("Device name is required")
        # Pre-auth input — cap the stored length (truncate, never reject; see
        # MAX_DEVICE_NAME_LENGTH). strip() again below so the cap can't leave a
        # trailing space.
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
                "enrolled_via": enrolled_via,
                # The PERSON this device belongs to. An "add device to member"
                # pairing code carries an existing member_id (the device joins
                # that person); a new-member code passes None and we mint one.
                # favorites + user_settings key by member_id so a person's
                # devices share them (auth itself still uses the per-device sub).
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
        """Idempotently enroll a device under a CALLER-CHOSEN id.

        The stable-id sibling of :meth:`enroll_device` (which mints a random,
        unguessable id). Here the caller names the id, so declarative
        provisioning — a manifest of trusted keys re-applied after every
        factory reset — can pin an id that OUTLIVES the reset, instead of a
        fresh random id breaking any tooling that hardcoded the old one.

        Sub-admin / user only: the single-admin invariant means an admin
        identity is never minted from a static manifest (same ceiling as
        :meth:`update_device`). Returns True when it actually wrote a record,
        False when the id is already enrolled with the SAME key/role/rooms — so
        it is safe to call on every boot and every auth-changed event and only
        ever writes on a real diff. If the id exists with DIFFERENT material the
        record is rewritten and its ``ver`` bumped, killing any stale tokens
        (same instant-revocation contract as :meth:`update_device`), so the
        manifest stays authoritative. The write keeps the in-memory cache in
        sync under the lock, so a freshly provisioned device can log in on the
        very next request — no :meth:`warm_up` needed.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise EnrollError("device_id is required")
        if not isinstance(name, str) or not name.strip():
            raise EnrollError("Device name is required")
        # Never admin: the dev/provisioning path must not be able to mint an
        # owner identity around the single-admin invariant.
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
                return False  # already correct — idempotent no-op

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
                # Recovery is replace_admin, not a code redemption — no code id.
                # Mirror the enroll record shape, which carries enrolled_via.
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
        """True once the hub's single admin is enrolled (cache read — cheap)."""
        with self._lock:
            return any(
                entry.get("role") == ROLE_ADMIN
                for entry in self._device_cache.values()
            )

    # -- user management (storage — call via executor) ---------------------------

    def list_devices(self) -> list[dict[str, Any]]:
        """Every enrolled device, public fields only (no keys).

        ``last_seen`` is the in-memory clock from the last successful token
        validation (None until the device makes its first authenticated call
        this boot — like the throttle counters, it is deliberately not
        persisted; a reboot just resets the liveness clock).
        """
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
        """Public fields for one enrolled device, or None when not enrolled.

        Cheap DB + cache read (call via executor). Used by ``/auth/whoami`` to
        report the device's CURRENT role/name and by the sensor platform.
        """
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
        """The enrolled device whose key matches ``public_key_pem``, or None.

        The same-phone re-pair seam: a phone re-running onboarding (a UI glitch,
        a redundant claim) sends the SAME public key it enrolled with. Matching
        it lets enroll be IDEMPOTENT — return the existing identity instead of
        bricking on an already-claimed hub. Safe: the returned id grants
        nothing; the token still requires proving the PRIVATE key via login.
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
                    }
        return None

    def member_id_for(self, device_id: str) -> str:
        """The PERSON id this device belongs to — the key favorites +
        user_settings roam by. Falls back to the device id for a legacy record
        enrolled before member_id existed (it is then its own one-device
        member), so no data migration is needed."""
        record = self._devices.get(device_id)
        if record is None:
            return device_id
        return record.get("member_id") or device_id

    def member_device_count(self, member_id: str) -> int:
        """How many enrolled devices belong to ``member_id`` — the caller
        prunes a member's personal data only when this hits zero (the last
        device unpaired)."""
        return sum(
            1
            for device_id, record in self._devices.items()
            if (record.get("member_id") or device_id) == member_id
        )

    def list_members(self) -> list[dict[str, Any]]:
        """Distinct members (people) with a device count — the family-share
        'add a device to [member]' picker. Name/role/rooms come from the
        member's most-recently-paired device (set consistently per member at
        pairing)."""
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
        """The device's last-validated-token timestamp, or None this boot.

        Pure in-memory read (no DB) — cheap enough for the user sensors to
        poll on the event loop. None until the device makes its first
        authenticated call since the last hub restart.
        """
        with self._lock:
            cached = self._device_cache.get(device_id)
            return cached.get("last_seen") if cached else None

    def device_for_token(self, token: str) -> dict[str, Any] | None:
        """Public device info for ``token``'s subject if STILL enrolled.

        Signature-verified but freshness-agnostic (see
        ``auth_tokens.unverified_subject``): an expired or version-bumped
        token whose device is still paired returns the device — the app's
        ``/auth/whoami`` resume check wants enrollment status, not token
        validity. Returns None for a forged token or an unpaired device.
        """
        device_id = auth_tokens.unverified_subject(self._signing_secret(), token)
        if device_id is None:
            return None
        return self.get_device(device_id)

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

    def delete_device(self, device_id: str) -> str:
        """Unpair a device — instant kill for all its tokens (plan B2).

        Returns the unpaired device's ``member_id`` so the caller can prune the
        person's favorites/user_settings IFF this was their last device (see
        :meth:`member_device_count`) — otherwise those rows orphan.

        The admin cannot be deleted through the API; factory reset is the
        only way the owner identity leaves the hub.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise UnknownDeviceError("Unknown device")
            if record.get("role") == ROLE_ADMIN:
                raise UserManagementError("The admin account cannot be unpaired")
            member_id = record.get("member_id") or device_id
            del self._devices[device_id]
            self._device_cache.pop(device_id, None)
            # Their login surface resets too — stale lockouts shouldn't
            # follow a re-pair of the same phone.
            self.throttle.clear(device_id)
        _LOGGER.info("Device %s unpaired — all tokens dead", device_id)
        return member_id

    def wipe_all_devices(self) -> list[str]:
        """Unpair EVERY device — admin included. The pairing factory reset.

        Unlike :meth:`delete_device` and :meth:`update_device`, there is NO
        admin guard here: wiping the owner IS the point. This hands the hub
        back to the unclaimed state, so the next :meth:`has_admin` is False and
        a fresh bootstrap admin code can be minted (the "Regenerate pairing
        code" button does exactly that). Every device's outstanding JWTs die
        instantly — its ``ver`` cache entry vanishes, same kill as an unpair —
        and each device's login-throttle counter is cleared so a re-pair of the
        same phone starts clean. Returns the wiped device ids.
        """
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
            # Liveness clock for the per-user sensors — in-memory, updated on
            # the same lock+read we already do, so no extra cost on the hot
            # path and no DB write per request.
            cached["last_seen"] = time.time()
            # Authorize off the STORED role/rooms, never the client-presented
            # JWT claim. The ver gate above already guarantees they match, but
            # stamping makes authorize() depend on the device record — so a token
            # whose claims were somehow trusted without this check can't ride an
            # elevated role past authorize().
            claims["role"] = cached["role"]
            claims["rooms"] = cached.get("rooms")
        return claims

    @staticmethod
    def authorize(claims: dict[str, Any], permission: str) -> bool:
        """True when the role grants the named permission.

        ``claims`` MUST come from ``AuthEngine.validate_token``, which stamps the
        role/rooms from the STORED device record (not the raw JWT) — so the role
        checked here is the device's, never a client-presented claim.

        A ``scope: widget`` token is additionally capped to
        ``WIDGET_SCOPE_PERMISSIONS`` — the scope check runs FIRST so a
        widget token held by an admin still can't reach admin surfaces.
        """
        if (
            claims.get("scope") == auth_tokens.SCOPE_WIDGET
            and permission not in WIDGET_SCOPE_PERMISSIONS
        ):
            return False
        allowed_roles = PERMISSIONS.get(permission)
        if allowed_roles is None:
            # Unknown permission = programming error; fail closed, loudly.
            _LOGGER.error("authorize() called with unknown permission %r", permission)
            return False
        return claims.get("role") in allowed_roles

    def mint_widget_token(self, device_id: str) -> dict[str, Any]:
        """Mint the long-lived, widget-scoped token for an enrolled device.

        Role/rooms/ver come from the device's CURRENT record — never from
        the requesting token — so the widget token always reflects the
        latest privilege edit and dies with the next one (``ver`` bump).
        Raises ``UnknownDeviceError`` when the device is gone.
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
