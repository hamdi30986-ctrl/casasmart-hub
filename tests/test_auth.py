"""Unit tests for the B1.6 auth engine (stdlib unittest + cryptography).

Run from the repo root:
    python3 -m unittest discover -s tests -v

``cryptography`` is an HA core dependency (present in every install);
locally it ships with macOS Python or pip. These tests use it both to
verify the engine AND to play the phone's part (generate a keypair, sign
nonces) — the full challenge-response round-trip without HTTP.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import auth_engine  # noqa: E402
import auth_keys  # noqa: E402
import auth_tokens  # noqa: E402
import throttle as throttle_mod  # noqa: E402
from auth_engine import (  # noqa: E402
    AuthEngine,
    ChallengeError,
    EnrollError,
    ThrottledError,
    UnknownDeviceError,
    UserManagementError,
)
from auth_tokens import TokenError  # noqa: E402
from storage import HubStorage, JsonConfigStore  # noqa: E402


def make_keypair():
    """A phone-side P-256 keypair: (private_key, public_pem)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


def sign_nonce(private_key, nonce: str) -> str:
    """What the app does: ECDSA-SHA256 over the nonce string, base64 DER."""
    import base64

    signature = private_key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


SECRET = bytes.fromhex(auth_tokens.generate_secret())


class TokenTests(unittest.TestCase):
    def test_round_trip(self):
        token = auth_tokens.issue_token(
            SECRET, "dev-1", "admin", None, ttl=3600
        )
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
            auth_tokens.validate_token(
                SECRET, f"{header}.{forged_claims}.{signature}"
            )

    def test_expired_rejected_with_skew(self):
        now = 1_000_000.0
        token = auth_tokens.issue_token(
            SECRET, "dev-1", "user", None, ttl=60, now=now
        )
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

    def test_alg_none_rejected(self):
        # Classic JWT attack: re-sign nothing, claim alg none.
        import base64
        import json

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        claims = base64.urlsafe_b64encode(
            json.dumps({"iss": auth_tokens.ISSUER, "sub": "x", "role": "admin",
                        "rooms": None, "iat": 0, "exp": 9_999_999_999}).encode()
        ).rstrip(b"=").decode()
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

        rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048) \
            .public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode()
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
        challenge = self.engine.create_challenge(device_id)
        return self.engine.redeem_challenge(
            device_id,
            challenge["challenge_id"],
            sign_nonce(self.private_key, challenge["nonce"]),
        )

    def test_full_login_round_trip(self):
        device_id = self.engine.enroll_device("Hamdi's iPhone", "admin", self.public_pem)
        issued = self._login(device_id)
        claims = self.engine.validate_token(issued["token"])
        self.assertEqual(claims["sub"], device_id)
        self.assertEqual(claims["role"], "admin")
        self.assertEqual(issued["expires_in"], auth_engine.TOKEN_TTL)

    def test_single_admin_rule(self):
        self.engine.enroll_device("First", "admin", self.public_pem)
        _, second_pem = make_keypair()
        with self.assertRaises(EnrollError):
            self.engine.enroll_device("Second", "admin", second_pem)
        # Other roles still fine.
        self.engine.enroll_device("Helper", "sub-admin", second_pem)

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
        challenge = self.engine.create_challenge(device_id)
        signature = sign_nonce(self.private_key, challenge["nonce"])
        self.engine.redeem_challenge(device_id, challenge["challenge_id"], signature)
        with self.assertRaises(ChallengeError):
            self.engine.redeem_challenge(
                device_id, challenge["challenge_id"], signature
            )

    def test_wrong_signature_rejected(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        challenge = self.engine.create_challenge(device_id)
        with self.assertRaises(ChallengeError):
            self.engine.redeem_challenge(
                device_id,
                challenge["challenge_id"],
                sign_nonce(self.private_key, "not-the-nonce"),
            )

    def test_unknown_device_challenge(self):
        with self.assertRaises(UnknownDeviceError):
            self.engine.create_challenge("dev-nope")

    def test_throttle_locks_after_failures(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        for _ in range(throttle_mod.MAX_FAILURES):
            challenge = self.engine.create_challenge(device_id)
            with self.assertRaises(ChallengeError):
                self.engine.redeem_challenge(
                    device_id, challenge["challenge_id"], "AAAA"
                )
        with self.assertRaises(ThrottledError) as ctx:
            self.engine.create_challenge(device_id)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_success_resets_throttle(self):
        device_id = self.engine.enroll_device("Phone", "user", self.public_pem)
        for _ in range(throttle_mod.MAX_FAILURES - 1):
            challenge = self.engine.create_challenge(device_id)
            with self.assertRaises(ChallengeError):
                self.engine.redeem_challenge(
                    device_id, challenge["challenge_id"], "AAAA"
                )
        self._login(device_id)  # success wipes the counter
        challenge = self.engine.create_challenge(device_id)  # no lockout
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

    # -- B2: user management + instant revocation -----------------------------

    def test_list_devices_has_no_keys(self):
        device_id = self.engine.enroll_device("Phone", "admin", self.public_pem)
        users = self.engine.list_devices()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["device_id"], device_id)
        self.assertEqual(users[0]["role"], "admin")
        self.assertNotIn("public_key", users[0])

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
        ):
            with self.subTest(permission=blocked):
                self.assertFalse(AuthEngine.authorize(claims, blocked))

    def test_widget_token_admin_still_capped(self):
        # An ADMIN's widget token must not reach admin surfaces.
        device_id = self.engine.enroll_device("Hamdi", "admin", self.public_pem)
        claims = self.engine.validate_token(
            self.engine.mint_widget_token(device_id)["token"]
        )
        self.assertFalse(AuthEngine.authorize(claims, "users.manage"))
        self.assertFalse(AuthEngine.authorize(claims, "automations.manage"))
        self.assertTrue(AuthEngine.authorize(claims, "devices.control"))

    def test_widget_token_inherits_room_scope(self):
        self.engine.enroll_device("Owner", "admin", self.public_pem)
        _, user_pem = make_keypair()
        user_id = self.engine.enroll_device(
            "Kid", "user", user_pem, rooms=["bedroom"]
        )
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

    def test_widget_token_unknown_device(self):
        with self.assertRaises(UnknownDeviceError):
            self.engine.mint_widget_token("ghost-device")

    def test_unknown_scope_claim_rejected(self):
        # A token whose scope claim isn't in VALID_SCOPES must not
        # validate into an unscoped (full) token.
        token = auth_tokens.issue_token(SECRET, "dev-1", "user", None, ttl=60)
        header_b64, claims_b64, _ = token.split(".")
        import base64 as b64
        import hmac as hmac_mod
        import hashlib as hashlib_mod
        import json as json_mod

        claims = json_mod.loads(
            b64.urlsafe_b64decode(claims_b64 + "=" * (-len(claims_b64) % 4))
        )
        claims["scope"] = "everything"
        new_claims_b64 = (
            b64.urlsafe_b64encode(
                json_mod.dumps(claims, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()
        )
        signing_input = f"{header_b64}.{new_claims_b64}".encode()
        signature = (
            b64.urlsafe_b64encode(
                hmac_mod.new(SECRET, signing_input, hashlib_mod.sha256).digest()
            ).rstrip(b"=").decode()
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
        ]
        for role, permission, expected in cases:
            with self.subTest(role=role, permission=permission):
                self.assertIs(
                    AuthEngine.authorize({"role": role}, permission), expected
                )
        # Unknown permission fails closed even for admin.
        self.assertFalse(AuthEngine.authorize({"role": "admin"}, "nuke.hub"))


if __name__ == "__main__":
    unittest.main()
