"""Unit tests for the auth engine (stdlib unittest + cryptography).

Run from the repo root:
    python3 -m unittest discover -s tests -v

``cryptography`` is an HA core dependency (present in every install);
locally it ships with macOS Python or pip. These tests use it both to
verify the engine AND to play the phone's part (generate a keypair, sign
nonces) — the full challenge-response round-trip without HTTP.
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

import auth_engine
import auth_keys
import auth_tokens
import throttle as throttle_mod
from auth_engine import (
    AuthEngine,
    ChallengeError,
    EnrollError,
    ThrottledError,
    UnknownDeviceError,
    UserManagementError,
)
from auth_tokens import TokenError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from storage import HubStorage, JsonConfigStore


def make_keypair():
    """A phone-side P-256 keypair: (private_key, public_pem)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_key, public_pem


# The address a test login comes from; the throttle keys on it.
SOURCE = "192.168.1.20"


def sign_nonce(private_key, nonce: str) -> str:
    """What the app does: ECDSA-SHA256 over the nonce string, base64 DER."""
    import base64

    signature = private_key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


SECRET = bytes.fromhex(auth_tokens.generate_secret())


class TokenTests(unittest.TestCase):
    def test_round_trip(self):
        token = auth_tokens.issue_token(SECRET, "dev-1", "admin", None, ttl=3600)
        claims = auth_tokens.validate_token(SECRET, token)
        self.assertEqual(claims["sub"], "dev-1")
        self.assertEqual(claims["role"], "admin")
        self.assertIsNone(claims["rooms"])
        self.assertEqual(claims["iss"], auth_tokens.ISSUER)

    def test_rooms_claim_survives(self):
        token = auth_tokens.issue_token(
            SECRET, "dev-2", "user", ["living", "kitchen"], ttl=60
        )
        claims = auth_tokens.validate_token(SECRET, token)
        self.assertEqual(claims["rooms"], ["living", "kitchen"])

    def test_wrong_secret_rejected(self):
        token = auth_tokens.issue_token(SECRET, "dev-1", "user", None, ttl=60)
        other = bytes.fromhex(auth_tokens.generate_secret())
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(other, token)

    def test_tampered_claims_rejected(self):
        token = auth_tokens.issue_token(SECRET, "dev-1", "user", None, ttl=60)
        header, claims, signature = token.split(".")
        forged_claims = claims[:-2] + ("AA" if claims[-2:] != "AA" else "BB")
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(SECRET, f"{header}.{forged_claims}.{signature}")

    def test_expired_rejected_with_skew(self):
        now = 1_000_000.0
        token = auth_tokens.issue_token(SECRET, "dev-1", "user", None, ttl=60, now=now)
        # Inside skew window -> still valid; past it -> expired.
        auth_tokens.validate_token(SECRET, token, now=now + 60 + 10)
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(
                SECRET, token, now=now + 60 + auth_tokens.CLOCK_SKEW + 1
            )

    def test_future_iat_rejected(self):
        token = auth_tokens.issue_token(
            SECRET, "dev-1", "user", None, ttl=60, now=2_000_000.0
        )
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(SECRET, token, now=1_000_000.0)

    def test_garbage_rejected(self):
        for garbage in ("", "a.b", "a.b.c.d", None, 42, "..", "a.b.!!!"):
            with self.assertRaises(TokenError):
                auth_tokens.validate_token(SECRET, garbage)

    def test_non_ascii_token_is_an_ordinary_bad_token(self):
        # An Authorization header or WebSocket auth frame can carry any text.
        # A non-ASCII token must fail like any other garbage (401, or a
        # refused socket), not escape as UnicodeEncodeError (an HTTP 500).
        token = auth_tokens.issue_token(SECRET, "dev-1", "admin", None, ttl=3600)
        header, claims, signature = token.split(".")
        for bad in (
            f"é{header}.{claims}.{signature}",
            f"{header}.{claims}é.{signature}",
            f"{header}.{claims}.{signature}é",
            f"{header}.\udcff.{signature}",
        ):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(TokenError):
                    auth_tokens.validate_token(SECRET, bad)
                self.assertIsNone(auth_tokens.unverified_subject(SECRET, bad))

    def test_alg_none_rejected(self):
        # Classic JWT attack: re-sign nothing, claim alg none.
        import base64
        import json

        header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            .rstrip(b"=")
            .decode()
        )
        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "iss": auth_tokens.ISSUER,
                        "sub": "x",
                        "role": "admin",
                        "rooms": None,
                        "iat": 0,
                        "exp": 9_999_999_999,
                    }
                ).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(SECRET, f"{header}.{claims}.")


class KeyTests(unittest.TestCase):
    def test_validate_and_verify_round_trip(self):
        private_key, public_pem = make_keypair()
        canonical = auth_keys.validate_public_key(public_pem)
        self.assertIn("BEGIN PUBLIC KEY", canonical)
        signature = sign_nonce(private_key, "nonce-123")
        self.assertTrue(auth_keys.verify_signature(canonical, "nonce-123", signature))
        # Wrong nonce / wrong key / garbage all fail closed.
        self.assertFalse(auth_keys.verify_signature(canonical, "other", signature))
        _, other_pem = make_keypair()
        self.assertFalse(auth_keys.verify_signature(other_pem, "nonce-123", signature))
        self.assertFalse(auth_keys.verify_signature(canonical, "nonce-123", "!!!"))

    def test_non_p256_keys_rejected(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        with self.assertRaises(auth_keys.KeyError_):
            auth_keys.validate_public_key(rsa_pem)
        with self.assertRaises(auth_keys.KeyError_):
            auth_keys.validate_public_key("not a key")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.storage = HubStorage(db_path=tmp / "hub.db")
        self.storage.open()
        self.engine = AuthEngine(
            self.storage.table("auth_devices"), JsonConfigStore(tmp / "cfg.json")
        )
        self.engine.warm_up()
        self.private_key, self.public_pem = make_keypair()

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _login(self, device_id):
        challenge = self.engine.create_challenge(device_id, SOURCE)
        return self.engine.redeem_challenge(
            device_id,
            challenge["challenge_id"],
            sign_nonce(self.private_key, challenge["nonce"]),
            SOURCE,
        )

    def test_full_login_round_trip(self):
        device_id = self.engine.enroll_device("Alex's iPhone", "admin", self.public_pem)
        issued = self._login(device_id)
        claims = self.engine.validate_token(issued["token"])
        self.assertEqual(claims["sub"], device_id)
        self.assertEqual(claims["role"], "admin")
        self.assertEqual(issued["expires_in"], auth_engine.TOKEN_TTL)

    def test_device_for_public_key_idempotent_re_pair(self):
        # The same-phone re-pair seam (the front-door fix): a phone re-running
        # onboarding sends the SAME public key it enrolled with. The hub must
        # RECOGNISE it — idempotent identity — instead of duplicating or, on a
        # claimed hub, bricking; the recognised id then logs in normally.
        device_id = self.engine.enroll_device("Owner", "admin", self.public_pem)
        found = self.engine.device_for_public_key(self.public_pem)
        self.assertIsNotNone(found)
        self.assertEqual(found["device_id"], device_id)
        self.assertEqual(found["role"], "admin")
        # The recognised identity actually authenticates (full round-trip).
        issued = self._login(found["device_id"])
        self.assertEqual(self.engine.validate_token(issued["token"])["sub"], device_id)
        # Canonicalisation: a re-encoded form of the same key still matches.
        self.assertIsNotNone(self.engine.device_for_public_key(self.public_pem + "\n"))
        # A DIFFERENT phone's key is unknown -> falls through to redeem.
        _, other_pem = make_keypair()
        self.assertIsNone(self.engine.device_for_public_key(other_pem))
        # Garbage never crashes.
        self.assertIsNone(self.engine.device_for_public_key("not-a-key"))

    def test_single_admin_rule(self):
        self.engine.enroll_device("First", "admin", self.public_pem)
        _, second_pem = make_keypair()
        with self.assertRaises(EnrollError):
            self.engine.enroll_device("Second", "admin", second_pem)
        # Other roles still fine.
        self.engine.enroll_device("Helper", "sub-admin", second_pem)

    def test_is_owner_device(self):
        # owner-only push (alarm/lock/tank) keys off this: the admin -> True; a
        # sub-admin / scoped / unknown device -> False.
        admin_id = self.engine.enroll_device("Owner", "admin", self.public_pem)
        _, sub_pem = make_keypair()
        sub_id = self.engine.enroll_device("Helper", "sub-admin", sub_pem)
        self.assertTrue(self.engine.is_owner_device(admin_id))
        self.assertFalse(self.engine.is_owner_device(sub_id))
        self.assertFalse(self.engine.is_owner_device("dev-nope"))

    def test_enroll_validation(self):
        with self.assertRaises(EnrollError):
            self.engine.enroll_device("", "admin", self.public_pem)
        with self.assertRaises(EnrollError):
            self.engine.enroll_device("X", "overlord", self.public_pem)
        with self.assertRaises(EnrollError):
            self.engine.enroll_device("X", "user", "garbage")
        with self.assertRaises(EnrollError):
            self.engine.enroll_device("X", "user", self.public_pem, rooms=[1, 2])

    def test_room_scope_lands_in_token(self):
        device_id = self.engine.enroll_device(
            "Kid's iPad", "user", self.public_pem, rooms=["area_living"]
        )
        claims = self.engine.validate_token(self._login(device_id)["token"])
        self.assertEqual(claims["rooms"], ["area_living"])

    def test_challenge_single_use(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        challenge = self.engine.create_challenge(device_id, SOURCE)
        signature = sign_nonce(self.private_key, challenge["nonce"])
        self.engine.redeem_challenge(
            device_id, challenge["challenge_id"], signature, SOURCE
        )
        with self.assertRaises(ChallengeError):
            self.engine.redeem_challenge(
                device_id, challenge["challenge_id"], signature, SOURCE
            )

    def test_wrong_signature_rejected(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        challenge = self.engine.create_challenge(device_id, SOURCE)
        with self.assertRaises(ChallengeError):
            self.engine.redeem_challenge(
                device_id,
                challenge["challenge_id"],
                sign_nonce(self.private_key, "not-the-nonce"),
                SOURCE,
            )

    def test_unknown_device_challenge(self):
        with self.assertRaises(UnknownDeviceError):
            self.engine.create_challenge("dev-nope", SOURCE)

    def test_throttle_locks_after_failures(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        for _ in range(throttle_mod.MAX_FAILURES):
            challenge = self.engine.create_challenge(device_id, SOURCE)
            with self.assertRaises(ChallengeError):
                self.engine.redeem_challenge(
                    device_id, challenge["challenge_id"], "AAAA", SOURCE
                )
        with self.assertRaises(ThrottledError) as ctx:
            self.engine.create_challenge(device_id, SOURCE)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_success_resets_throttle(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        for _ in range(throttle_mod.MAX_FAILURES - 1):
            challenge = self.engine.create_challenge(device_id, SOURCE)
            with self.assertRaises(ChallengeError):
                self.engine.redeem_challenge(
                    device_id, challenge["challenge_id"], "AAAA", SOURCE
                )
        self._login(device_id)  # success wipes the counter
        challenge = self.engine.create_challenge(device_id, SOURCE)  # no lockout
        self.assertIn("nonce", challenge)

    def test_secret_persists_across_engines(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self._login(device_id)["token"]
        # New engine over the same storage/config = hub restart.
        engine2 = AuthEngine(
            self.storage.table("auth_devices"),
            JsonConfigStore(Path(self._tmp.name) / "cfg.json"),
        )
        engine2.warm_up()
        claims = engine2.validate_token(token)
        self.assertEqual(claims["sub"], device_id)

    # -- User management + instant revocation ---------------------------------

    def test_list_devices_has_no_keys(self):
        device_id = self.engine.enroll_device("Phone", "admin", self.public_pem)
        users = self.engine.list_devices()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["device_id"], device_id)
        self.assertEqual(users[0]["role"], "admin")
        self.assertNotIn("public_key", users[0])

    def test_enrolled_via_recorded(self):
        device_id = self.engine.enroll_device(
            "Guest", "user", self.public_pem, enrolled_via="pair-abc"
        )
        listed = next(
            u for u in self.engine.list_devices() if u["device_id"] == device_id
        )
        self.assertEqual(listed["enrolled_via"], "pair-abc")
        self.assertEqual(self.engine.get_device(device_id)["enrolled_via"], "pair-abc")

    def test_last_seen_tracked_on_validate(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        # No authenticated traffic yet -> no liveness clock.
        self.assertIsNone(self.engine.get_device(device_id)["last_seen"])
        self.assertIsNone(self.engine.last_seen(device_id))
        self.engine.validate_token(self._login(device_id)["token"])
        self.assertIsInstance(self.engine.get_device(device_id)["last_seen"], float)
        self.assertIsInstance(self.engine.last_seen(device_id), float)
        # Unknown device -> None, never a KeyError.
        self.assertIsNone(self.engine.last_seen("dev-nope"))

    def test_wipe_all_devices(self):
        admin = self.engine.enroll_device("Owner", "admin", self.public_pem)
        _, pem_a = make_keypair()
        _, pem_b = make_keypair()
        sub = self.engine.enroll_device("Helper", "sub-admin", pem_a)
        user = self.engine.enroll_device("Kid", "user", pem_b)
        # The wipe spares no one — admin included (this IS the reset).
        self.assertEqual(set(self.engine.wipe_all_devices()), {admin, sub, user})
        self.assertEqual(self.engine.list_devices(), [])
        self.assertFalse(self.engine.has_admin())

    def test_wipe_all_devices_kills_tokens_instantly(self):
        device_id = self.engine.enroll_device("Phone", "admin", self.public_pem)
        token = self._login(device_id)["token"]
        self.engine.validate_token(token)  # valid before
        self.engine.wipe_all_devices()
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)  # dead after, no TTL wait

    def test_wipe_all_devices_clears_throttle(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        for source in (SOURCE, "192.168.1.21"):
            challenge = self.engine.create_challenge(device_id, source)
            with self.assertRaises(ChallengeError):
                self.engine.redeem_challenge(
                    device_id, challenge["challenge_id"], "AAAA", source
                )
        self.assertEqual(len(self.engine.throttle._entries), 2)
        self.engine.wipe_all_devices()
        self.assertEqual(self.engine.throttle._entries, {})

    def test_wipe_all_devices_resets_to_unclaimed(self):
        self.engine.enroll_device("Owner", "admin", self.public_pem)
        self.assertTrue(self.engine.has_admin())
        self.engine.wipe_all_devices()
        # Hub is unclaimed again -> a fresh admin can enroll (invariant reset).
        self.assertFalse(self.engine.has_admin())
        _, new_pem = make_keypair()
        self.engine.enroll_device("New Owner", "admin", new_pem)
        self.assertTrue(self.engine.has_admin())

    def test_wipe_all_devices_empty_is_noop(self):
        self.assertEqual(self.engine.wipe_all_devices(), [])

    def test_device_for_token_enrolled(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        info = self.engine.device_for_token(self._login(device_id)["token"])
        self.assertEqual(info["device_id"], device_id)
        self.assertEqual(info["role"], "user")

    def test_device_for_token_ignores_expiry(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        expired = auth_tokens.issue_token(
            self.engine._signing_secret(), device_id, "user", None, ttl=-100
        )
        with self.assertRaises(TokenError):
            self.engine.validate_token(expired)  # validate rejects it...
        # ...but the enrollment probe still recognizes the still-paired device.
        self.assertEqual(self.engine.device_for_token(expired)["device_id"], device_id)

    def test_device_for_token_unpaired_and_forged(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self._login(device_id)["token"]
        self.engine.delete_device(device_id)
        self.assertIsNone(self.engine.device_for_token(token))  # unpaired
        self.assertIsNone(self.engine.device_for_token("a.b.c"))  # forged

    def test_get_device_unknown(self):
        self.assertIsNone(self.engine.get_device("dev-nope"))

    def test_unverified_subject_requires_our_signature(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self._login(device_id)["token"]
        secret = self.engine._signing_secret()
        self.assertEqual(auth_tokens.unverified_subject(secret, token), device_id)
        # A token signed with a different secret must not be recognized.
        self.assertIsNone(auth_tokens.unverified_subject(b"\x00" * 32, token))
        self.assertIsNone(auth_tokens.unverified_subject(secret, "garbage"))

    def test_delete_kills_tokens_instantly(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self._login(device_id)["token"]
        self.engine.validate_token(token)  # valid before
        self.engine.delete_device(device_id)
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)  # dead after, no TTL wait
        with self.assertRaises(UnknownDeviceError):
            self.engine.delete_device(device_id)

    def test_role_change_kills_tokens_instantly(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self._login(device_id)["token"]
        updated = self.engine.update_device(device_id, role="sub-admin")
        self.assertEqual(updated["role"], "sub-admin")
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)  # old privileges revoked
        # Fresh login carries the new role.
        claims = self.engine.validate_token(self._login(device_id)["token"])
        self.assertEqual(claims["role"], "sub-admin")

    def test_room_scope_edit(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        updated = self.engine.update_device(device_id, rooms=["area_kitchen"])
        self.assertEqual(updated["rooms"], ["area_kitchen"])
        claims = self.engine.validate_token(self._login(device_id)["token"])
        self.assertEqual(claims["rooms"], ["area_kitchen"])
        # Explicit None clears the scope; sentinel default leaves it alone.
        self.assertEqual(
            self.engine.update_device(device_id, rooms=None)["rooms"], None
        )
        self.assertEqual(
            self.engine.update_device(device_id, role="user")["rooms"], None
        )

    def test_admin_is_untouchable(self):
        device_id = self.engine.enroll_device("Owner", "admin", self.public_pem)
        with self.assertRaises(UserManagementError):
            self.engine.update_device(device_id, role="user")
        with self.assertRaises(UserManagementError):
            self.engine.delete_device(device_id)
        self.assertTrue(self.engine.has_admin())

    def test_leave_hub_lets_the_admin_hand_the_hub_back(self):
        # The counterpart to test_admin_is_untouchable: an admin cannot be
        # EVICTED by someone else, but it can remove ITSELF. Without this the
        # owner's "Remove Hub" was unrecoverable — the hub kept the phone
        # enrolled as its one admin, and a hub with an admin never mints
        # another, so no phone could administer it again short of the recovery
        # card or the physical reset button.
        device_id = self.engine.enroll_device("Owner", "admin", self.public_pem)
        token = self._login(device_id)["token"]
        self.assertTrue(self.engine.has_admin())

        self.engine.leave_hub(device_id)

        self.assertFalse(self.engine.has_admin())
        self.assertIsNone(self.engine.get_device(device_id))
        # Its tokens die instantly, same as any unpair.
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)
        # And the hub is claimable again by a fresh admin enrollment.
        self.engine.enroll_device("New Owner", "admin", self.public_pem)
        self.assertTrue(self.engine.has_admin())

    def test_leave_hub_is_not_a_way_to_evict_anyone_else(self):
        # leave_hub is only ever called with the caller's OWN device id (the
        # view takes it from the token subject, never from the body), so the
        # admin guard it skips can't be turned on a third party. Removing a
        # non-admin behaves exactly like the admin-driven unpair.
        _, other_pem = make_keypair()
        admin_id = self.engine.enroll_device("Owner", "admin", self.public_pem)
        user_id = self.engine.enroll_device("Kid", "user", other_pem)

        self.engine.leave_hub(user_id)

        self.assertIsNone(self.engine.get_device(user_id))
        self.assertIsNotNone(self.engine.get_device(admin_id))
        self.assertTrue(self.engine.has_admin())

    def test_leave_hub_unknown_device(self):
        with self.assertRaises(UnknownDeviceError):
            self.engine.leave_hub("dev-nope")

    def test_no_promotion_to_admin(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        with self.assertRaises(UserManagementError):
            self.engine.update_device(device_id, role="admin")

    def test_rooms_only_for_user_role(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        with self.assertRaises(UserManagementError):
            self.engine.update_device(
                device_id, role="sub-admin", rooms=["area_living"]
            )

    def test_revocation_survives_restart(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self._login(device_id)["token"]
        self.engine.update_device(device_id, role="sub-admin")
        engine2 = AuthEngine(
            self.storage.table("auth_devices"),
            JsonConfigStore(Path(self._tmp.name) / "cfg.json"),
        )
        engine2.warm_up()  # cache rebuilt from storage — ver came with it
        with self.assertRaises(TokenError):
            engine2.validate_token(token)

    def test_widget_token_mint_and_scope(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        issued = self.engine.mint_widget_token(device_id)
        self.assertEqual(issued["scope"], "widget")
        self.assertEqual(issued["expires_in"], auth_engine.WIDGET_TOKEN_TTL)
        claims = self.engine.validate_token(issued["token"])
        self.assertEqual(claims["scope"], "widget")
        self.assertEqual(claims["sub"], device_id)
        # The widget set, and ONLY the widget set.
        self.assertTrue(AuthEngine.authorize(claims, "devices.read"))
        self.assertTrue(AuthEngine.authorize(claims, "devices.control"))
        for blocked in (
            "history.read",
            "cameras.view",
            "automations.manage",
            "users.manage",
            "pairing.generate",
            "registry.manage",
            "widget.token",  # no self-renewal
            "session.manage",  # no push re-registration, no self-unpair
        ):
            with self.subTest(permission=blocked):
                self.assertFalse(AuthEngine.authorize(claims, blocked))

    def test_session_manage_is_every_session_but_no_widget(self):
        # Push registration and self-unpair act on the device's identity: every
        # role's SESSION may do it, a widget token (whatever its role) may not.
        for role in ("admin", "sub-admin", "user"):
            with self.subTest(role=role):
                session = {"sub": "dev-1", "role": role}
                widget = {**session, "scope": "widget"}
                self.assertTrue(AuthEngine.authorize(session, "session.manage"))
                self.assertFalse(AuthEngine.authorize(widget, "session.manage"))
        self.assertNotIn("session.manage", auth_engine.WIDGET_SCOPE_PERMISSIONS)

    def test_widget_token_admin_still_capped(self):
        # An ADMIN's widget token must not reach admin surfaces.
        device_id = self.engine.enroll_device("Alex", "admin", self.public_pem)
        claims = self.engine.validate_token(
            self.engine.mint_widget_token(device_id)["token"]
        )
        self.assertFalse(AuthEngine.authorize(claims, "users.manage"))
        self.assertFalse(AuthEngine.authorize(claims, "automations.manage"))
        self.assertTrue(AuthEngine.authorize(claims, "devices.control"))

    def test_widget_token_inherits_room_scope(self):
        self.engine.enroll_device("Owner", "admin", self.public_pem)
        _, user_pem = make_keypair()
        user_id = self.engine.enroll_device("Kid", "user", user_pem, rooms=["bedroom"])
        claims = self.engine.validate_token(
            self.engine.mint_widget_token(user_id)["token"]
        )
        self.assertEqual(claims["rooms"], ["bedroom"])

    def test_widget_token_revoked_on_edit(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        token = self.engine.mint_widget_token(device_id)["token"]
        self.engine.update_device(device_id, rooms=["living"])  # ver bump
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)

    def test_widget_token_revoked_on_unpair(self):
        self.engine.enroll_device("Owner", "admin", self.public_pem)
        _, user_pem = make_keypair()
        user_id = self.engine.enroll_device("Phone", "user", user_pem)
        token = self.engine.mint_widget_token(user_id)["token"]
        self.engine.delete_device(user_id)
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)

    def test_token_error_code_stale_vs_unenrolled(self):
        # The 401 `code` contract (widget-kick incident 2026-07-09): an
        # edited device's outstanding tokens are STALE (recover via
        # login/re-mint); only a deleted device is UNENROLLED (the one
        # code that means "pair again"). Session and widget tokens must
        # agree.
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        session = self._login(device_id)["token"]
        widget = self.engine.mint_widget_token(device_id)["token"]

        self.engine.update_device(device_id, rooms=["living"])  # ver bump
        for stale in (session, widget):
            with self.assertRaises(TokenError) as ctx:
                self.engine.validate_token(stale)
            self.assertEqual(ctx.exception.code, "token_stale")

        self.engine.delete_device(device_id)
        for dead in (session, widget):
            with self.assertRaises(TokenError) as ctx:
                self.engine.validate_token(dead)
            self.assertEqual(ctx.exception.code, "unenrolled")

    def test_token_error_code_invalid_and_expired(self):
        with self.assertRaises(TokenError) as ctx:
            self.engine.validate_token("garbage")
        self.assertEqual(ctx.exception.code, "token_invalid")

        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        expired = auth_tokens.issue_token(
            self.engine._signing_secret(), device_id, "user", None, ttl=-100
        )
        with self.assertRaises(TokenError) as ctx:
            self.engine.validate_token(expired)
        self.assertEqual(ctx.exception.code, "token_expired")

    def test_widget_token_unknown_device(self):
        with self.assertRaises(UnknownDeviceError):
            self.engine.mint_widget_token("ghost-device")

    def test_unknown_scope_claim_rejected(self):
        # A token whose scope claim isn't in VALID_SCOPES must not
        # validate into an unscoped (full) token.
        token = auth_tokens.issue_token(SECRET, "dev-1", "user", None, ttl=60)
        header_b64, claims_b64, _ = token.split(".")
        import base64 as b64
        import hashlib as hashlib_mod
        import hmac as hmac_mod
        import json as json_mod

        claims = json_mod.loads(
            b64.urlsafe_b64decode(claims_b64 + "=" * (-len(claims_b64) % 4))
        )
        claims["scope"] = "everything"
        new_claims_b64 = (
            b64.urlsafe_b64encode(
                json_mod.dumps(claims, separators=(",", ":")).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        signing_input = f"{header_b64}.{new_claims_b64}".encode()
        signature = (
            b64.urlsafe_b64encode(
                hmac_mod.new(SECRET, signing_input, hashlib_mod.sha256).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        forged = f"{header_b64}.{new_claims_b64}.{signature}"
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(SECRET, forged)

    def test_authorize_matrix(self):
        cases = [
            ("admin", "users.manage", True),
            ("admin", "devices.control", True),
            ("sub-admin", "users.manage", False),
            ("sub-admin", "devices.control", True),
            ("user", "pairing.generate", False),
            ("user", "devices.read", True),
            # Audio: read+control are household-wide, manage is admin tier.
            ("user", "audio.read", True),
            ("user", "audio.control", True),
            ("user", "audio.manage", False),
            ("sub-admin", "audio.manage", True),
            ("admin", "audio.manage", True),
        ]
        for role, permission, expected in cases:
            with self.subTest(role=role, permission=permission):
                self.assertIs(
                    AuthEngine.authorize({"role": role}, permission), expected
                )
        # Unknown permission fails closed even for admin.
        self.assertFalse(AuthEngine.authorize({"role": "admin"}, "nuke.hub"))


class LoopSideReadTests(unittest.TestCase):
    """Token checks run on the event loop, so they never wait for a write.

    Writes hold the engine lock across their SQLite commit, which a slow
    disk can stretch to hundreds of milliseconds.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.storage = HubStorage(db_path=tmp / "hub.db")
        self.storage.open()
        self.engine = AuthEngine(
            self.storage.table("auth_devices"), JsonConfigStore(tmp / "cfg.json")
        )
        self.engine.warm_up()
        self.owner = self.engine.enroll_device("Owner", "admin", make_keypair()[1])
        self.kid = self.engine.enroll_device("Kid", "user", make_keypair()[1])

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _token(self, device_id, role):
        return auth_tokens.issue_token(
            self.engine._signing_secret(), device_id, role, None, ttl=600, ver=1
        )

    def test_reads_do_not_wait_for_a_commit_in_progress(self):
        owner_token = self._token(self.owner, "admin")
        kid_token = self._token(self.kid, "user")
        committing = threading.Event()
        finish = threading.Event()
        real_write = HubStorage._execute_write

        def _slow_write(storage, sql, params=()):
            committing.set()
            finish.wait(2)
            return real_write(storage, sql, params)

        with mock.patch.object(HubStorage, "_execute_write", _slow_write):
            writer = threading.Thread(
                target=self.engine.update_device,
                args=(self.kid,),
                kwargs={"rooms": ["bedroom"]},
            )
            writer.start()
            self.assertTrue(committing.wait(2))
            started = time.monotonic()
            self.assertEqual(self.engine.validate_token(owner_token)["sub"], self.owner)
            # The edit hasn't committed, so the kid's token still holds.
            self.assertEqual(self.engine.validate_token(kid_token)["rooms"], None)
            self.assertTrue(self.engine.is_owner_device(self.owner))
            self.assertIsNotNone(self.engine.last_seen(self.owner))
            elapsed = time.monotonic() - started
            finish.set()
            writer.join(5)

        self.assertLess(elapsed, 0.5)
        with self.assertRaises(TokenError):
            self.engine.validate_token(kid_token)  # stale once committed


if __name__ == "__main__":
    unittest.main()


class CameraAccessTests(unittest.TestCase):
    """Per-member camera access: none / rooms / all, admin always all.

    A record from before the field existed keeps today's behaviour (cameras
    follow the room scope), so the upgrade locks nobody out.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.storage = HubStorage(db_path=tmp / "hub.db")
        self.storage.open()
        self.engine = AuthEngine(
            self.storage.table("auth_devices"), JsonConfigStore(tmp / "cfg.json")
        )
        self.engine.warm_up()

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _pair(self, name, role, **kwargs):
        """Enroll a fresh phone; returns (device_id, private_key, public_pem)."""
        private_key, public_pem = make_keypair()
        device_id = self.engine.enroll_device(name, role, public_pem, **kwargs)
        return device_id, private_key, public_pem

    def _login(self, device_id, private_key):
        challenge = self.engine.create_challenge(device_id, SOURCE)
        return self.engine.redeem_challenge(
            device_id,
            challenge["challenge_id"],
            sign_nonce(private_key, challenge["nonce"]),
            SOURCE,
        )

    def test_effective_access_defaults_follow_the_room_scope(self):
        self.assertEqual(auth_tokens.effective_camera_access(None, None), "all")
        self.assertEqual(auth_tokens.effective_camera_access(None, ["k"]), "rooms")
        self.assertEqual(auth_tokens.effective_camera_access(None, []), "rooms")
        self.assertEqual(auth_tokens.effective_camera_access("none", None), "none")
        # Garbage in storage is treated as unset, never as a grant.
        self.assertEqual(
            auth_tokens.effective_camera_access("everything", ["k"]), "rooms"
        )

    def test_record_without_the_field_keeps_todays_behaviour(self):
        unscoped, _, _ = self._pair("Phone", "user")
        scoped, scoped_key, _ = self._pair("Kid", "user", rooms=["bedroom"])
        # Stored as unset, reported as the behaviour it has.
        self.assertIsNone(self.engine._devices[unscoped]["camera_access"])
        self.assertEqual(self.engine.get_device(unscoped)["camera_access"], "all")
        self.assertEqual(self.engine.get_device(scoped)["camera_access"], "rooms")
        # A record written by an older hub has no key at all.
        legacy = dict(self.engine._devices[scoped])
        del legacy["camera_access"]
        self.engine._devices[scoped] = legacy
        self.engine.warm_up()
        self.assertEqual(self.engine.get_device(scoped)["camera_access"], "rooms")
        issued = self._login(scoped, scoped_key)
        self.assertEqual(issued["camera_access"], "rooms")
        self.assertEqual(issued["rooms"], ["bedroom"])
        claims = self.engine.validate_token(issued["token"])
        self.assertEqual(claims["camera_access"], "rooms")

    def test_old_token_without_the_claim_gets_the_records_access(self):
        device_id, _, _ = self._pair("Phone", "user", camera_access="none")
        # What a pre-upgrade hub minted: no camera_access claim at all.
        old = auth_tokens.issue_token(
            self.engine._signing_secret(), device_id, "user", None, ttl=600, ver=1
        )
        self.assertEqual(self.engine.validate_token(old)["camera_access"], "none")

    def test_explicit_access_is_stored_and_lands_in_the_token(self):
        device_id, key, _ = self._pair("Phone", "user", camera_access="none")
        self.assertEqual(self.engine.get_device(device_id)["camera_access"], "none")
        issued = self._login(device_id, key)
        self.assertEqual(issued["camera_access"], "none")
        self.assertIsNone(issued["rooms"])
        claims = self.engine.validate_token(issued["token"])
        self.assertEqual(claims["camera_access"], "none")
        # Also in the signed claims themselves (the holder can read its own).
        raw = auth_tokens.validate_token(self.engine._signing_secret(), issued["token"])
        self.assertEqual(raw["camera_access"], "none")

    def test_update_changes_access_and_revokes_tokens(self):
        device_id, key, _ = self._pair("Phone", "user")
        token = self._login(device_id, key)["token"]
        updated = self.engine.update_device(device_id, camera_access="none")
        self.assertEqual(updated["camera_access"], "none")
        with self.assertRaises(TokenError):
            self.engine.validate_token(token)
        fresh = self._login(device_id, key)["token"]
        self.assertEqual(self.engine.validate_token(fresh)["camera_access"], "none")
        # The sentinel default leaves it alone; a room edit keeps it.
        self.engine.update_device(device_id, rooms=["kitchen"])
        self.assertEqual(self.engine.get_device(device_id)["camera_access"], "none")
        self.engine.update_device(device_id, camera_access="all")
        self.assertEqual(self.engine.get_device(device_id)["camera_access"], "all")
        self.assertEqual(self.engine.get_device(device_id)["rooms"], ["kitchen"])

    def test_bad_values_are_refused(self):
        _, public_pem = make_keypair()
        with self.assertRaises(EnrollError):
            self.engine.enroll_device(
                "Phone", "user", public_pem, camera_access="everything"
            )
        device_id, _, _ = self._pair("Phone", "user")
        with self.assertRaises(UserManagementError):
            self.engine.update_device(device_id, camera_access="some")
        with self.assertRaises(UserManagementError):
            self.engine.update_device(device_id, camera_access=None)
        with self.assertRaises(ValueError):
            auth_tokens.issue_token(
                SECRET, "dev-1", "user", None, ttl=60, camera_access="some"
            )

    def test_malformed_claim_in_a_token_is_rejected(self):
        import base64
        import hashlib
        import hmac
        import json

        token = auth_tokens.issue_token(SECRET, "dev-1", "user", None, ttl=3600)
        header, claims, _ = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(claims + "=="))
        decoded["camera_access"] = "everything"
        forged = (
            base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
        )
        signing_input = f"{header}.{forged}".encode()
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(SECRET, signing_input, hashlib.sha256).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        with self.assertRaises(TokenError):
            auth_tokens.validate_token(SECRET, f"{header}.{forged}.{signature}")

    def test_admin_always_has_every_camera(self):
        admin_id, key, _ = self._pair("Owner", "admin")
        self.assertEqual(self.engine.get_device(admin_id)["camera_access"], "all")
        # Even a stray value in storage can't take the owner's cameras away.
        record = self.engine._devices[admin_id]
        record["camera_access"] = "none"
        self.engine._devices[admin_id] = record
        self.engine.warm_up()
        self.assertEqual(self.engine.get_device(admin_id)["camera_access"], "all")
        claims = self.engine.validate_token(self._login(admin_id, key)["token"])
        self.assertEqual(claims["camera_access"], "all")

    def test_widget_token_carries_the_access(self):
        device_id, _, _ = self._pair("Phone", "user", camera_access="none")
        claims = self.engine.validate_token(
            self.engine.mint_widget_token(device_id)["token"]
        )
        self.assertEqual(claims["camera_access"], "none")

    def test_members_and_public_key_lookup_report_the_access(self):
        device_id, _, public_pem = self._pair(
            "Phone", "user", rooms=["k"], camera_access="all"
        )
        member = self.engine.list_members()[0]
        self.assertEqual(member["camera_access"], "all")
        self.assertEqual(member["rooms"], ["k"])
        known = self.engine.device_for_public_key(public_pem)
        self.assertEqual(known["device_id"], device_id)
        self.assertEqual(known["camera_access"], "all")

    def test_prune_rooms_drops_dead_ids_without_revoking(self):
        self._pair("Owner", "admin")
        kid, kid_key, _ = self._pair("Kid", "user", rooms=["kitchen", "garage"])
        guest, _, _ = self._pair("Guest", "user", rooms=["garage"])
        free, _, _ = self._pair("Free", "user")
        token = self._login(kid, kid_key)["token"]
        ver_before = self.engine.device_version(kid)

        changed = self.engine.prune_rooms({"kitchen", "living"})

        self.assertEqual(
            sorted((d["device_id"], d["rooms"]) for d in changed),
            sorted([(kid, ["kitchen"]), (guest, [])]),
        )
        self.assertEqual(self.engine.get_device(kid)["rooms"], ["kitchen"])
        self.assertEqual(self.engine.get_device(guest)["rooms"], [])
        self.assertIsNone(self.engine.get_device(free)["rooms"])
        # No revocation: a dead id granted nothing, so nothing changed for
        # the holder; every check reads the new scope from the cache.
        self.assertEqual(self.engine.device_version(kid), ver_before)
        self.assertEqual(self.engine.validate_token(token)["rooms"], ["kitchen"])
        # Idempotent.
        self.assertEqual(self.engine.prune_rooms({"kitchen", "living"}), [])
        # Survives a restart.
        self.engine.warm_up()
        self.assertEqual(self.engine._device_cache[guest]["rooms"], [])

    def test_authorize_still_grants_cameras_view_to_every_role(self):
        # The permission stays role-wide; the per-member access is enforced by
        # filtering.visible_to on every camera path, not by authorize().
        for role in ("admin", "sub-admin", "user"):
            self.assertTrue(AuthEngine.authorize({"role": role}, "cameras.view"))
