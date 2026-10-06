"""Device public-key handling (Track B — B1.6): P-256 validate + verify.

The only module that touches the ``cryptography`` package (an HA core
dependency — present in every HA install, nothing to add to the
manifest). Keeping the crypto in one seam means the engine and the API
layer stay testable and the primitive is swappable in one place.

Contract with the app (plan, "Phone Identity"):

- Phone generates a P-256 keypair on first launch; the PUBLIC key crosses
  the wire once, at pairing, as SubjectPublicKeyInfo PEM.
- Every login: hub hands out a one-time nonce, phone signs the exact
  nonce string (UTF-8 bytes) with ECDSA-SHA256, sends the DER signature
  base64-encoded. The private key never travels.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class KeyError_(Exception):
    """The supplied public key is not a usable P-256 key."""


def validate_public_key(public_key_pem: str) -> str:
    """Validate an enrollment key; return it re-serialized in canonical PEM.

    Re-serializing (instead of storing the client's bytes verbatim) strips
    any garbage around the PEM body and guarantees that what's in storage
    always loads back.
    """
    if not isinstance(public_key_pem, str) or "BEGIN PUBLIC KEY" not in public_key_pem:
        raise KeyError_("Expected a PEM-encoded public key (SubjectPublicKeyInfo)")
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
    except (ValueError, UnsupportedAlgorithm) as err:
        raise KeyError_(f"Unparseable public key: {err}") from err
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise KeyError_("Key must be ECDSA P-256 (secp256r1)")
    return key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def verify_signature(public_key_pem: str, nonce: str, signature_b64: str) -> bool:
    """True iff ``signature_b64`` is a valid ECDSA-SHA256 signature of the
    nonce string by the holder of the stored public key. Never raises on
    bad input — a malformed signature is just a failed verification."""
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return False
    if not isinstance(key, ec.EllipticCurvePublicKey):
        return False
    try:
        key.verify(signature, nonce.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True
