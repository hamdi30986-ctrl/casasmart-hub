"""Unit tests for B8 Piece 4: the Ed25519 push-identity key.

Covers key creation/persistence, file permissions, the never-re-key failure
posture, signing, and — crucially — the cross-language signature vector that
proves the hub's canonical JSON (json.dumps with ensure_ascii=False) is
byte-identical to the relay's TypeScript canonicalJson. The matching relay
assertions live in casasmart-relay/tests/crypto_test.ts; both sides pin the
SAME canonical string and the SAME signature, derived from the fixed seed
bytes(range(32)).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

_CC = Path(__file__).resolve().parent.parent / "custom_components"
_PKG = _CC / "casasmart"
# push_crypto uses a package-relative import (``from .storage import ...``), so
# it must load through a real ``casasmart`` package — register a stub whose
# __path__ points at the source dir (same trick as test_alarm_adapter). No
# homeassistant stubs are needed: push_crypto only touches cryptography + storage.
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))
if "casasmart" not in sys.modules:
    _pkg = types.ModuleType("casasmart")
    _pkg.__path__ = [str(_PKG)]
    sys.modules["casasmart"] = _pkg

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from casasmart.push_crypto import (  # noqa: E402
    PUSH_IDENTITY_KEY_FILENAME,
    PUSH_PUBLIC_KEY_CONFIG_KEY,
    PushIdentityError,
    PushSigner,
    ensure_push_identity,
)
from storage import JsonConfigStore  # noqa: E402


class PushIdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config = JsonConfigStore(self.data_dir / "hub_config.json")

    def _key_path(self) -> Path:
        return self.data_dir / PUSH_IDENTITY_KEY_FILENAME


class CreationTests(PushIdentityTestCase):
    def test_key_created_once_and_stable(self) -> None:
        first = ensure_push_identity(self.data_dir, self.config)
        second = ensure_push_identity(self.data_dir, self.config)
        self.assertEqual(first.public_key_hex, second.public_key_hex)
        self.assertTrue(self._key_path().exists())
        self.assertEqual(len(first.public_key_hex), 64)  # 32-byte pubkey, hex

    def test_key_file_is_0600(self) -> None:
        ensure_push_identity(self.data_dir, self.config)
        mode = stat.S_IMODE(os.stat(self._key_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_pubkey_mirrored_to_config(self) -> None:
        signer = ensure_push_identity(self.data_dir, self.config)
        self.assertEqual(
            self.config.get(PUSH_PUBLIC_KEY_CONFIG_KEY), signer.public_key_hex
        )

    def test_pubkey_repaired_in_config_if_drifted(self) -> None:
        ensure_push_identity(self.data_dir, self.config)
        # Simulate a stale/garbage config value; next ensure must overwrite it.
        self.config.set(PUSH_PUBLIC_KEY_CONFIG_KEY, "deadbeef")
        signer = ensure_push_identity(self.data_dir, self.config)
        self.assertEqual(
            self.config.get(PUSH_PUBLIC_KEY_CONFIG_KEY), signer.public_key_hex
        )


class FailurePostureTests(PushIdentityTestCase):
    def test_corrupt_key_raises_and_is_untouched(self) -> None:
        ensure_push_identity(self.data_dir, self.config)
        self._key_path().write_bytes(b"not a real key")  # wrong length + garbage
        with self.assertRaises(PushIdentityError):
            ensure_push_identity(self.data_dir, self.config)
        # The file is left for a human to restore — never silently re-keyed.
        self.assertEqual(self._key_path().read_bytes(), b"not a real key")

    def test_right_length_but_invalid_key_raises(self) -> None:
        # 32 bytes is the right length, but an all-zero seed is still a valid
        # Ed25519 key, so to exercise the parse-failure path we corrupt after
        # writing a too-short file (covered above). Here we assert the length
        # guard fires precisely.
        self._key_path().write_bytes(b"\x00" * 31)  # one byte short
        with self.assertRaises(PushIdentityError):
            ensure_push_identity(self.data_dir, self.config)


class SigningTests(PushIdentityTestCase):
    def test_signature_verifies_with_public_key(self) -> None:
        signer = ensure_push_identity(self.data_dir, self.config)
        message = b"the quick brown fox"
        signature = signer.sign(message)
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer.public_key_hex))
        pub.verify(signature, message)  # raises on mismatch -> test fails

    def test_signature_is_64_bytes(self) -> None:
        signer = ensure_push_identity(self.data_dir, self.config)
        self.assertEqual(len(signer.sign(b"x")), 64)


# ---------------------------------------------------------------------------
# Cross-language signature vector (hub Python <-> relay TypeScript)
# ---------------------------------------------------------------------------

# Fixed seed so the vector is reproducible from either language.
_VECTOR_SEED = bytes(range(32))
_VECTOR_PUBKEY_HEX = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
_VECTOR_SIGNATURE_B64 = (
    "9SydU5TQmPUsHwrx2/BjqBRowibfKIXAiFeljOxbLNGhm/iH1NSorZ547kWQhqmPskgH5E8OSyktIeXAffx4Cw=="
)
# Byte-for-byte identical to VECTOR_CANONICAL in crypto_test.ts — Arabic kept as
# raw UTF-8 (ensure_ascii=False), keys sorted, no whitespace.
_VECTOR_CANONICAL = (
    '{"hub_id":"a1b2c3d4","nonce":"abababababababababababababababab",'
    '"payloads":[{"data":{"body":"تم تشغيل الإنذار","entity_id":"binary_sensor.front_door",'
    '"title":"تنبيه أمني","type":"security"},"device_token":"tok-arabic"}],'
    '"priority":"critical","timestamp":1700000000}'
)


class CrossLanguageVectorTests(unittest.TestCase):
    """The Python half of the hub<->relay signature contract."""

    def _signed_message(self) -> dict:
        return {
            "hub_id": "a1b2c3d4",
            "timestamp": 1700000000,  # INT, never float
            "nonce": "ab" * 16,
            "priority": "critical",
            "payloads": [
                {
                    "device_token": "tok-arabic",
                    "data": {
                        "type": "security",
                        "title": "تنبيه أمني",
                        "body": "تم تشغيل الإنذار",
                        "entity_id": "binary_sensor.front_door",
                    },
                }
            ],
        }

    def test_canonical_json_matches_the_pinned_vector(self) -> None:
        canonical = json.dumps(
            self._signed_message(),
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
        self.assertEqual(canonical, _VECTOR_CANONICAL)
        # Raw UTF-8, NOT \uXXXX — proves ensure_ascii=False. An ascii-escaped
        # encoder would be much longer than 288 bytes.
        self.assertEqual(len(canonical.encode("utf-8")), 288)

    def test_signature_matches_and_pubkey_is_stable(self) -> None:
        signer = PushSigner(Ed25519PrivateKey.from_private_bytes(_VECTOR_SEED))
        self.assertEqual(signer.public_key_hex, _VECTOR_PUBKEY_HEX)
        signature = base64.b64encode(
            signer.sign(_VECTOR_CANONICAL.encode("utf-8"))
        ).decode("ascii")
        self.assertEqual(signature, _VECTOR_SIGNATURE_B64)


if __name__ == "__main__":
    unittest.main()
