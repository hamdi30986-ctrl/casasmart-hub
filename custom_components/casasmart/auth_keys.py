"""CasaSmart runtime component."""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class KeyError_(Exception):
    """CasaSmart runtime component."""


def validate_public_key(public_key_pem: str) -> str:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
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
