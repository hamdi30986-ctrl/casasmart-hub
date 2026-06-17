"""Unit tests for the dev-only auto-enrollment seam (``dev_enroll.py``) and the
stable-id :meth:`AuthEngine.ensure_enrolled` it rides on.

Run from the repo root (same as the rest of the suite):
    python3 -m unittest discover -s tests -v
    # or: python -m pytest tests/ -x -q

``cryptography`` (an HA core dependency) plays the phone's part — generate a
keypair, sign nonces — so the tests exercise the FULL challenge-response login
of a provisioned device, not just that a row landed in storage.
"""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import auth_keys  # noqa: E402
import dev_enroll  # noqa: E402
from auth_engine import AuthEngine, EnrollError  # noqa: E402
from auth_tokens import ROLE_SUB_ADMIN, ROLE_USER, TokenError  # noqa: E402
from storage import HubStorage, JsonConfigStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = REPO_ROOT / ".dev"


def make_keypair():
    """A phone-side P-256 keypair: (private_key, public_pem)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


def sign_nonce(private_key, nonce: str) -> str:
    """ECDSA-SHA256 over the nonce string, base64 DER — what the app does."""
    return base64.b64encode(
        private_key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))
    ).decode()


class _EngineFixture(unittest.TestCase):
    """Shared temp storage + warmed AuthEngine + a keypair."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.storage = HubStorage(db_path=self.data_dir / "hub.db")
        self.storage.open()
        self.engine = AuthEngine(
            self.storage.table("auth_devices"),
            JsonConfigStore(self.data_dir / "cfg.json"),
        )
        self.engine.warm_up()
        self.private_key, self.public_pem = make_keypair()

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def login(self, device_id, private_key=None):
        """Full challenge-response round-trip; returns the issued token dict."""
        challenge = self.engine.create_challenge(device_id)
        return self.engine.redeem_challenge(
            device_id,
            challenge["challenge_id"],
            sign_nonce(private_key or self.private_key, challenge["nonce"]),
        )


class EnsureEnrolledTests(_EngineFixture):
    """The stable-id, idempotent engine primitive."""

    def test_provisions_under_caller_chosen_id_and_logs_in(self):
        created = self.engine.ensure_enrolled(
            "dev-fixed", "dev tooling", ROLE_SUB_ADMIN, self.public_pem
        )
        self.assertTrue(created)
        device = self.engine.get_device("dev-fixed")
        self.assertEqual(device["device_id"], "dev-fixed")
        self.assertEqual(device["role"], ROLE_SUB_ADMIN)
        # Cache stayed in sync under the lock — login works with NO warm_up().
        claims = self.engine.validate_token(self.login("dev-fixed")["token"])
        self.assertEqual(claims["sub"], "dev-fixed")
        self.assertEqual(claims["role"], ROLE_SUB_ADMIN)

    def test_idempotent_identical_record_is_noop(self):
        self.assertTrue(
            self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, self.public_pem)
        )
        # Second identical call writes nothing and reports no change.
        self.assertFalse(
            self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, self.public_pem)
        )
        self.assertEqual(len(self.engine.list_devices()), 1)
        # ver did NOT bump (a bump would needlessly kill live tokens).
        token = self.login("dev-x")["token"]
        self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, self.public_pem)
        self.engine.validate_token(token)  # still valid — no exception

    def test_rekey_same_id_bumps_ver_and_kills_old_token(self):
        self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, self.public_pem)
        old_token = self.login("dev-x")["token"]
        # Re-point the SAME id at a DIFFERENT key (manifest stays authoritative).
        new_private, new_pem = make_keypair()
        self.assertTrue(
            self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, new_pem)
        )
        # Old token is dead (ver bump), the new key logs in.
        with self.assertRaises(TokenError):
            self.engine.validate_token(old_token)
        claims = self.engine.validate_token(self.login("dev-x", new_private)["token"])
        self.assertEqual(claims["sub"], "dev-x")

    def test_role_change_same_id_rewrites(self):
        self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, self.public_pem)
        self.assertTrue(
            self.engine.ensure_enrolled("dev-x", "n", ROLE_USER, self.public_pem)
        )
        self.assertEqual(self.engine.get_device("dev-x")["role"], ROLE_USER)

    def test_user_role_carries_room_scope(self):
        self.assertTrue(
            self.engine.ensure_enrolled(
                "dev-room", "n", ROLE_USER, self.public_pem, rooms=["living_room"]
            )
        )
        self.assertEqual(self.engine.get_device("dev-room")["rooms"], ["living_room"])

    def test_refuses_admin_role(self):
        with self.assertRaises(EnrollError):
            self.engine.ensure_enrolled("dev-x", "n", "admin", self.public_pem)
        self.assertEqual(self.engine.list_devices(), [])

    def test_input_validation(self):
        with self.assertRaises(EnrollError):  # empty id
            self.engine.ensure_enrolled("", "n", ROLE_SUB_ADMIN, self.public_pem)
        with self.assertRaises(EnrollError):  # empty name
            self.engine.ensure_enrolled("dev-x", "", ROLE_SUB_ADMIN, self.public_pem)
        with self.assertRaises(EnrollError):  # unknown role
            self.engine.ensure_enrolled("dev-x", "n", "overlord", self.public_pem)
        with self.assertRaises(EnrollError):  # garbage key
            self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, "garbage")
        with self.assertRaises(EnrollError):  # malformed rooms
            self.engine.ensure_enrolled(
                "dev-x", "n", ROLE_USER, self.public_pem, rooms=[1, 2]
            )

    def test_does_not_disturb_the_single_admin_invariant(self):
        # A real admin can still be enrolled alongside a provisioned sub-admin.
        admin_private, admin_pem = make_keypair()
        self.engine.enroll_device("Owner", "admin", admin_pem)
        self.assertTrue(
            self.engine.ensure_enrolled("dev-x", "n", ROLE_SUB_ADMIN, self.public_pem)
        )
        self.assertTrue(self.engine.has_admin())
        self.assertEqual(len(self.engine.list_devices()), 2)


class DevEnrollManifestTests(_EngineFixture):
    """The file-gated orchestration: read manifest -> provision -> idempotent."""

    def _write_manifest(self, entries):
        path = self.data_dir / dev_enroll.DEV_DEVICES_FILENAME
        path.write_text(json.dumps(entries))
        return path

    def _run(self):
        """Run the seam against ONLY the temp data dir (hermetic — ignores the
        real repo .dev kit so the suite is reproducible anywhere)."""
        with mock.patch.object(
            dev_enroll,
            "_candidate_paths",
            return_value=[self.data_dir / dev_enroll.DEV_DEVICES_FILENAME],
        ):
            return dev_enroll.ensure_dev_devices(self.data_dir, self.engine)

    def test_no_manifest_is_a_noop(self):
        self.assertEqual(self._run(), [])
        self.assertEqual(self.engine.list_devices(), [])

    def test_provisions_subadmin_from_manifest_and_logs_in(self):
        self._write_manifest(
            [
                {
                    "device_id": "dev-AcjPJAjpO8s7ORjd",
                    "label": "dev tooling",
                    "role": "sub-admin",
                    "public_key": self.public_pem,
                }
            ]
        )
        self.assertEqual(self._run(), ["dev-AcjPJAjpO8s7ORjd"])
        device = self.engine.get_device("dev-AcjPJAjpO8s7ORjd")
        self.assertEqual(device["role"], ROLE_SUB_ADMIN)
        # mint_token.py's exact flow: challenge-response as the fixed id.
        claims = self.engine.validate_token(
            self.login("dev-AcjPJAjpO8s7ORjd")["token"]
        )
        self.assertEqual(claims["role"], ROLE_SUB_ADMIN)

    def test_default_role_is_subadmin_when_omitted(self):
        self._write_manifest([{"device_id": "dev-x", "public_key": self.public_pem}])
        self._run()
        self.assertEqual(self.engine.get_device("dev-x")["role"], ROLE_SUB_ADMIN)

    def test_idempotent_second_run_writes_nothing(self):
        self._write_manifest([{"device_id": "dev-x", "public_key": self.public_pem}])
        self.assertEqual(self._run(), ["dev-x"])
        self.assertEqual(self._run(), [])
        self.assertEqual(len(self.engine.list_devices()), 1)

    def test_survives_factory_reset(self):
        """THE guarantee: after a button-style wipe, re-running the seam
        restores the dev identity and it logs in again — zero manual steps."""
        self._write_manifest(
            [{"device_id": "dev-x", "public_key": self.public_pem, "role": "sub-admin"}]
        )
        self._run()
        self.login("dev-x")  # works pre-reset

        # The "Regenerate pairing code" button path wipes every device in place.
        self.engine.wipe_all_devices()
        self.assertIsNone(self.engine.get_device("dev-x"))
        with self.assertRaises(Exception):
            self.login("dev-x")  # gone

        # The EVENT_AUTH_CHANGED listener re-runs the seam -> identity restored.
        self.assertEqual(self._run(), ["dev-x"])
        claims = self.engine.validate_token(self.login("dev-x")["token"])
        self.assertEqual(claims["sub"], "dev-x")

    def test_admin_entry_is_skipped_others_still_enroll(self):
        good_private, good_pem = make_keypair()
        self._write_manifest(
            [
                {"device_id": "dev-evil", "role": "admin", "public_key": self.public_pem},
                {"device_id": "dev-good", "role": "sub-admin", "public_key": good_pem},
            ]
        )
        self.assertEqual(self._run(), ["dev-good"])
        self.assertIsNone(self.engine.get_device("dev-evil"))
        self.assertFalse(self.engine.has_admin())

    def test_deterministic_id_when_device_id_omitted(self):
        self._write_manifest([{"public_key": self.public_pem, "label": "anon"}])
        changed = self._run()
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0].startswith("dev-"))
        # Stable across runs -> idempotent (no second device).
        self.assertEqual(self._run(), [])
        self.assertEqual(len(self.engine.list_devices()), 1)

    def test_accepts_design_doc_field_aliases(self):
        # public_key_pem / name (the plan's names) are accepted too.
        self._write_manifest(
            [{"device_id": "dev-x", "public_key_pem": self.public_pem, "name": "alias"}]
        )
        self.assertEqual(self._run(), ["dev-x"])
        self.assertEqual(self.engine.get_device("dev-x")["name"], "alias")

    def test_room_scoped_user_entry(self):
        self._write_manifest(
            [
                {
                    "device_id": "dev-room",
                    "role": "user",
                    "rooms": ["living_room"],
                    "public_key": self.public_pem,
                }
            ]
        )
        self._run()
        self.assertEqual(
            self.engine.get_device("dev-room")["rooms"], ["living_room"]
        )

    def test_bad_entries_are_skipped_not_fatal(self):
        good_private, good_pem = make_keypair()
        self._write_manifest(
            [
                "not-an-object",
                {"label": "no key"},
                {"device_id": "dev-bad", "public_key": "garbage"},
                {"device_id": "dev-bad2", "public_key": self.public_pem, "rooms": [1]},
                {"device_id": "dev-ok", "public_key": good_pem},
            ]
        )
        self.assertEqual(self._run(), ["dev-ok"])
        self.assertEqual(len(self.engine.list_devices()), 1)

    def test_malformed_manifest_files_do_not_crash(self):
        path = self.data_dir / dev_enroll.DEV_DEVICES_FILENAME
        for bad in ("{not json", json.dumps({"not": "a list"}), ""):
            path.write_text(bad)
            self.assertEqual(self._run(), [])
        self.assertEqual(self.engine.list_devices(), [])


class CandidatePathTests(unittest.TestCase):
    """The fallback-location logic, asserted without depending on disk."""

    def test_searches_data_dir_then_dev_kit(self):
        data_dir = Path("/some/hub/casasmart")
        paths = dev_enroll._candidate_paths(data_dir)
        self.assertEqual(paths[0], data_dir / "dev_devices.json")
        # Second candidate is <repo_root>/.dev/dev_devices.json, resolved
        # relative to the module so host repo and container both find it.
        self.assertEqual(paths[1].parent.name, ".dev")
        self.assertEqual(paths[1].name, "dev_devices.json")
        self.assertEqual(
            paths[1],
            Path(dev_enroll.__file__).resolve().parents[2] / ".dev" / "dev_devices.json",
        )


@unittest.skipUnless(
    (DEV_DIR / "dev_devices.json").is_file()
    and (DEV_DIR / "admin_key.pem").is_file()
    and (DEV_DIR / "admin_device_id").is_file(),
    "real .dev proof kit not present",
)
class CommittedManifestIntegrityTests(unittest.TestCase):
    """Guard the REAL committed manifest against drift: it must correspond to
    the real private key AND to the device id mint_token.py authenticates as —
    otherwise mint_token.py would silently break after a reset."""

    def test_manifest_matches_admin_key_and_device_id(self):
        manifest = json.loads((DEV_DIR / "dev_devices.json").read_text())
        self.assertIsInstance(manifest, list)
        by_id = {e.get("device_id"): e for e in manifest}

        expected_id = (DEV_DIR / "admin_device_id").read_text().strip()
        self.assertIn(
            expected_id,
            by_id,
            f"mint_token.py uses device id {expected_id!r}; manifest must list it",
        )
        entry = by_id[expected_id]
        self.assertEqual(entry.get("role"), "sub-admin")

        priv = serialization.load_pem_private_key(
            (DEV_DIR / "admin_key.pem").read_bytes(), None
        )
        derived = priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        manifest_key = entry.get("public_key") or entry.get("public_key_pem")
        self.assertEqual(
            auth_keys.validate_public_key(manifest_key),
            auth_keys.validate_public_key(derived),
            "manifest public_key does not match .dev/admin_key.pem",
        )


if __name__ == "__main__":
    unittest.main()
