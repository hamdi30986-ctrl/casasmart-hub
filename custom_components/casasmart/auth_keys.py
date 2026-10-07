"""Device public keys: validate a P-256 key and verify login signatures.

The phone creates a P-256 keypair on first launch and sends the public key
once, at pairing, as SubjectPublicKeyInfo PEM. At each login it signs the
hub's nonce string (UTF-8) with ECDSA-SHA256 and sends the DER signature
base64-encoded; the private key never leaves the phone. cryptography ships
with Home Assistant, so the manifest needs no extra requirement.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class KeyError_(Exception):
    """The supplied public key is not a usable P-256 key.

    The trailing underscore keeps it from shadowing the builtin KeyError.
    """


def validate_public_key(public_key_pem: str) -> str:
    """Validate an enrollment key and return it as canonical PEM.

    Storing the re-serialized key drops anything around the PEM body and
    guarantees the stored value loads back.
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
    """Whether signature_b64 is the key holder's ECDSA-SHA256 signature of nonce.

    Malformed input returns False instead of raising.
    """
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
