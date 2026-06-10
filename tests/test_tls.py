"""Tests for tls.py — identity key + leaf cert lifecycle (B10).

Covers the material layer only (pure crypto + files); the aiohttp
listener is verified live against the dev hub, like the other servers.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from tls import (  # noqa: E402
    IDENTITY_KEY_FILENAME,
    TLS_CERT_FILENAME,
    TLS_KEY_FILENAME,
    IdentityError,
    ensure_tls_material,
)


class TlsMaterialTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- identity ---------------------------------------------------------------

    def test_identity_created_once_and_stable(self) -> None:
        first = ensure_tls_material(self.data_dir)
        second = ensure_tls_material(self.data_dir)
        self.assertEqual(first.identity_fingerprint, second.identity_fingerprint)
        self.assertEqual(first.identity_public_pem, second.identity_public_pem)
        self.assertTrue(
            first.identity_public_pem.startswith("-----BEGIN PUBLIC KEY-----")
        )
        self.assertEqual(len(first.identity_fingerprint), 64)  # sha256 hex

    def test_identity_key_file_is_0600(self) -> None:
        ensure_tls_material(self.data_dir)
        mode = stat.S_IMODE(
            os.stat(self.data_dir / IDENTITY_KEY_FILENAME).st_mode
        )
        self.assertEqual(mode, 0o600)

    def test_corrupt_identity_raises_never_rekeys(self) -> None:
        ensure_tls_material(self.data_dir)
        key_path = self.data_dir / IDENTITY_KEY_FILENAME
        key_path.write_bytes(b"not a pem")
        with self.assertRaises(IdentityError):
            ensure_tls_material(self.data_dir)
        # The corrupt file is untouched — recovery is a human decision.
        self.assertEqual(key_path.read_bytes(), b"not a pem")

    def test_non_p256_identity_refused(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (self.data_dir / IDENTITY_KEY_FILENAME).write_bytes(
            rsa_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        with self.assertRaises(IdentityError):
            ensure_tls_material(self.data_dir)

    # -- leaf -------------------------------------------------------------------

    def _load_cert(self) -> x509.Certificate:
        return x509.load_pem_x509_certificate(
            (self.data_dir / TLS_CERT_FILENAME).read_bytes()
        )

    def test_leaf_signed_by_identity(self) -> None:
        material = ensure_tls_material(self.data_dir)
        self.assertTrue(material.leaf_rotated)
        cert = self._load_cert()
        identity_pub = serialization.load_pem_public_key(
            material.identity_public_pem.encode()
        )
        identity_pub.verify(  # raises on mismatch
            cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256())
        )
        # Leaf key is its OWN keypair, not the identity key.
        leaf_spki = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        identity_spki = identity_pub.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.assertNotEqual(leaf_spki, identity_spki)

    def test_leaf_validity_window(self) -> None:
        material = ensure_tls_material(self.data_dir, validity_days=365)
        cert = self._load_cert()
        now = datetime.now(timezone.utc)
        self.assertLess(cert.not_valid_before_utc, now)  # backdated
        self.assertAlmostEqual(
            (material.cert_not_after - now).days, 365, delta=1
        )
        self.assertEqual(material.cert_not_after, cert.not_valid_after_utc)

    def test_good_leaf_reused(self) -> None:
        first = ensure_tls_material(self.data_dir)
        second = ensure_tls_material(self.data_dir)
        self.assertFalse(second.leaf_rotated)
        self.assertEqual(first.cert_not_after, second.cert_not_after)

    def test_expiring_leaf_rotated(self) -> None:
        # Inside the 30-day renewal margin from the start.
        ensure_tls_material(self.data_dir, validity_days=10)
        renewed = ensure_tls_material(self.data_dir)
        self.assertTrue(renewed.leaf_rotated)
        self.assertGreater(
            renewed.cert_not_after,
            datetime.now(timezone.utc) + timedelta(days=300),
        )

    def test_foreign_signed_leaf_rotated(self) -> None:
        """A leaf signed by some OTHER identity gets re-minted."""
        ensure_tls_material(self.data_dir)
        with tempfile.TemporaryDirectory() as other_dir:
            other = Path(other_dir)
            ensure_tls_material(other)
            (self.data_dir / TLS_CERT_FILENAME).write_bytes(
                (other / TLS_CERT_FILENAME).read_bytes()
            )
            (self.data_dir / TLS_KEY_FILENAME).write_bytes(
                (other / TLS_KEY_FILENAME).read_bytes()
            )
        renewed = ensure_tls_material(self.data_dir)
        self.assertTrue(renewed.leaf_rotated)
        # And the fresh one verifies against OUR identity again.
        cert = self._load_cert()
        identity_pub = serialization.load_pem_public_key(
            renewed.identity_public_pem.encode()
        )
        identity_pub.verify(
            cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256())
        )

    def test_garbage_leaf_rotated(self) -> None:
        ensure_tls_material(self.data_dir)
        (self.data_dir / TLS_CERT_FILENAME).write_bytes(b"junk")
        renewed = ensure_tls_material(self.data_dir)
        self.assertTrue(renewed.leaf_rotated)

    def test_key_cert_mismatch_rotated(self) -> None:
        ensure_tls_material(self.data_dir)
        stray = ec.generate_private_key(ec.SECP256R1())
        (self.data_dir / TLS_KEY_FILENAME).write_bytes(
            stray.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        renewed = ensure_tls_material(self.data_dir)
        self.assertTrue(renewed.leaf_rotated)

    def test_leaf_key_file_is_0600(self) -> None:
        ensure_tls_material(self.data_dir)
        mode = stat.S_IMODE(os.stat(self.data_dir / TLS_KEY_FILENAME).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
