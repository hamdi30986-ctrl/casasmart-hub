"""Unit tests for B3: owner recovery codes + admin replacement.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

import recovery as recovery_mod  # noqa: E402
from auth_engine import AuthEngine, EnrollError  # noqa: E402
from auth_tokens import TokenError  # noqa: E402
from recovery import (  # noqa: E402
    RECOVERY_CODE_ID,
    CodeInvalidError,
    RecoveryManager,
    normalize_code,
)
from storage import HubStorage, JsonConfigStore  # noqa: E402
from test_auth import make_keypair, sign_nonce  # noqa: E402
from throttle import ThrottledError  # noqa: E402


class RecoveryManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(db_path=Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self._admin = True
        self.manager = RecoveryManager(
            self.storage.table("recovery_codes"), lambda: self._admin
        )

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_arm_and_redeem(self):
        code = self.manager.ensure_armed()
        # XXXXX-XXXXX from the unambiguous alphabet only.
        self.assertRegex(code, r"^[2-9A-HJKMNP-Z]{5}-[2-9A-HJKMNP-Z]{5}$")
        self.assertTrue(self.manager.is_armed())
        self.manager.redeem(code, "ip-1")  # no raise = success
        # PERMANENT (decision 3a): the engraved card stays valid after redeem.
        self.assertTrue(self.manager.is_armed())

    def test_arm_is_idempotent(self):
        code = self.manager.ensure_armed()
        self.assertIsNotNone(code)
        # Already armed -> no new code, the existing one stays valid.
        self.assertIsNone(self.manager.ensure_armed())
        self.manager.redeem(code, "ip-1")

    def test_unclaimed_hub_never_arms_and_drops_stale_code(self):
        code = self.manager.ensure_armed()
        self._admin = False  # factory reset: admin gone, old code on disk
        self.assertIsNone(self.manager.ensure_armed())
        self.assertFalse(self.manager.is_armed())  # stale code dropped
        self._admin = True
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem(code, "ip-1")  # previous owner's card is dead

    def test_permanent_reusable(self):
        # PERMANENT (decision 3a): the recovery card is reusable, not single-use —
        # a second redeem of the same code succeeds and the code stays armed.
        code = self.manager.ensure_armed()
        self.manager.redeem(code, "ip-1")
        self.manager.redeem(code, "ip-1")  # no raise = still valid
        self.assertTrue(self.manager.is_armed())

    def test_mint_permanent_is_reusable(self):
        # First-provisioning mint returns plaintext once; the code is permanent.
        code = self.manager.mint_permanent()
        self.assertRegex(code, r"^[2-9A-HJKMNP-Z]{5}-[2-9A-HJKMNP-Z]{5}$")
        self.manager.redeem(code, "ip-1")
        self.manager.redeem(code, "ip-1")  # reusable
        self.assertTrue(self.manager.is_armed())

    def test_install_recovery_hash_makes_code_redeemable(self):
        # On every boot the permanent code is re-installed from its stored hash;
        # the original card then validates against the re-installed record.
        from recovery import hash_code  # noqa: PLC0415

        code = "ABCDE-23456"
        self.manager.install_recovery_hash(hash_code(code))
        self.assertTrue(self.manager.is_armed())
        self.manager.redeem(code, "ip-1")  # installed hash validates the card

    def test_redeem_normalizes_input(self):
        code = self.manager.ensure_armed()
        sloppy = f"  {code.replace('-', ' ').lower()} "
        self.manager.redeem(sloppy, "ip-1")  # spaces/case/dashes don't matter

    def test_wrong_or_empty_code_rejected(self):
        self.manager.ensure_armed()
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem("AAAAA-AAAAA", "ip-1")
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem("", "ip-1")
        self.assertTrue(self.manager.is_armed())  # failures don't consume

    def test_guessing_gets_throttled(self):
        self.manager.ensure_armed()
        with self.assertRaises(ThrottledError):
            for _ in range(10):
                try:
                    self.manager.redeem("WRONG-GUESS", "ip-evil")
                except CodeInvalidError:
                    continue
        # Other sources are unaffected.
        with self.assertRaises(CodeInvalidError):
            self.manager.redeem("WRONG-GUESS", "ip-clean")

    def test_no_plaintext_at_rest(self):
        code = self.manager.ensure_armed()
        record = self.storage.table("recovery_codes")[RECOVERY_CODE_ID]
        self.assertNotIn(normalize_code(code), str(record))
        self.assertEqual(len(record["code_hash"]), 64)  # sha256 hex

    def test_code_survives_restart(self):
        code = self.manager.ensure_armed()
        reopened = RecoveryManager(
            self.storage.table("recovery_codes"), lambda: True
        )
        self.assertIsNone(reopened.ensure_armed())  # still armed, no re-mint
        reopened.redeem(code, "ip-1")


class ReplaceAdminTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.storage = HubStorage(db_path=tmp / "hub.db")
        self.storage.open()
        self.engine = AuthEngine(
            self.storage.table("auth_devices"), JsonConfigStore(tmp / "cfg.json")
        )
        self.engine.warm_up()
        self.old_key, self.old_pem = make_keypair()
        self.new_key, self.new_pem = make_keypair()
        self.admin_id = self.engine.enroll_device("Old Phone", "admin", self.old_pem)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _login(self, device_id, private_key):
        challenge = self.engine.create_challenge(device_id)
        return self.engine.redeem_challenge(
            device_id,
            challenge["challenge_id"],
            sign_nonce(private_key, challenge["nonce"]),
        )

    def test_replace_admin_swaps_identity_and_kills_tokens(self):
        old_token = self._login(self.admin_id, self.old_key)["token"]
        self.engine.validate_token(old_token)  # valid before recovery

        new_id = self.engine.replace_admin("New Phone", self.new_pem)
        self.assertNotEqual(new_id, self.admin_id)
        with self.assertRaises(TokenError):
            self.engine.validate_token(old_token)  # old admin's JWT is dead
        # Old device id is gone entirely; new phone logs in as admin.
        ids = [d["device_id"] for d in self.engine.list_devices()]
        self.assertNotIn(self.admin_id, ids)
        issued = self._login(new_id, self.new_key)
        self.assertEqual(issued["role"], "admin")
        self.assertTrue(self.engine.has_admin())

    def test_replace_admin_validates_before_touching_old_admin(self):
        with self.assertRaises(EnrollError):
            self.engine.replace_admin("New Phone", "garbage-key")
        with self.assertRaises(EnrollError):
            self.engine.replace_admin("", self.new_pem)
        # Bad input never leaves the hub adminless.
        self.assertTrue(self.engine.has_admin())
        self.assertEqual(self._login(self.admin_id, self.old_key)["role"], "admin")

    def test_replace_admin_requires_an_admin(self):
        self.engine.replace_admin("New Phone", self.new_pem)
        # Wipe everything (factory-reset shape) -> recovery has no target.
        for device in self.engine.list_devices():
            self.storage.table("auth_devices").pop(device["device_id"])
        self.engine.warm_up()
        with self.assertRaises(EnrollError):
            self.engine.replace_admin("Another", self.old_pem)

    def test_other_devices_survive_recovery(self):
        user_key, user_pem = make_keypair()
        user_id = self.engine.enroll_device("Kid", "user", user_pem, rooms=["a1"])
        user_token = self._login(user_id, user_key)["token"]
        self.engine.replace_admin("New Phone", self.new_pem)
        # Only the admin was swapped — the user's token still validates.
        claims = self.engine.validate_token(user_token)
        self.assertEqual(claims["sub"], user_id)


if __name__ == "__main__":
    unittest.main()
