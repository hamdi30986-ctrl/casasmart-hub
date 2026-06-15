"""Ed25519 push-identity key for B8 relay dispatch.

The hub signs every push batch it sends to the relay with a permanent
Ed25519 key generated on first boot. The relay verifies that signature
against the hub's public key, which is registered with the relay
**out-of-band** — the hub never self-registers (plan B8). The private
key never leaves the box (``push_identity_key.bin``, 0600); the public
key (lowercase hex) is mirrored into ``hub_config`` so the registration
tooling and the admin API have a stable place to read it.

This is a deliberately SEPARATE key from the P-256 TLS identity in
``tls.py``: that one pins the LAN certificate, this one authenticates
relay pushes. Different algorithm, different job, different blast radius
if either ever leaks.

Failure posture mirrors the TLS identity: a corrupt key is a hard error
(``PushIdentityError``), never silently re-generated. Re-keying would
orphan the public key already registered with the relay, so every push
would start failing signature verification — a human must decide to
delete the file and re-register.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from .storage import JsonConfigStore

_LOGGER = logging.getLogger(__name__)

# Raw 32-byte Ed25519 private key (seed). Lives next to the TLS identity
# under <ha-config>/casasmart/.
PUSH_IDENTITY_KEY_FILENAME = "push_identity_key.bin"
# hub_config key holding the public key (lowercase hex, 64 chars) so the
# out-of-band relay registration can read it without unsealing the file.
PUSH_PUBLIC_KEY_CONFIG_KEY = "push_public_key"

# Ed25519 raw private/public keys are always exactly 32 bytes.
_ED25519_KEY_BYTES = 32


class PushIdentityError(Exception):
    """The permanent push-identity key is unusable — never auto-recovered."""


class PushSigner:
    """Signs canonical push messages with the hub's Ed25519 identity key."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self._public_key_hex = private_key.public_key().public_bytes_raw().hex()

    @property
    def public_key_hex(self) -> str:
        """Lowercase hex of the 32-byte Ed25519 public key (64 chars)."""
        return self._public_key_hex

    def sign(self, message: bytes) -> bytes:
        """Return the raw 64-byte Ed25519 signature over ``message``."""
        return self._private_key.sign(message)


def _load_or_create_identity(key_path: Path) -> Ed25519PrivateKey:
    """Load the raw key file, or mint + persist one at 0600 on first boot."""
    if key_path.exists():
        raw = key_path.read_bytes()
        if len(raw) != _ED25519_KEY_BYTES:
            raise PushIdentityError(
                f"Push identity key at {key_path} is {len(raw)} bytes, expected "
                f"{_ED25519_KEY_BYTES}. Restore it from backup, or delete the file "
                "to re-key — re-keying needs the hub re-registered with the relay."
            )
        try:
            return Ed25519PrivateKey.from_private_bytes(raw)
        except (ValueError, UnsupportedAlgorithm) as err:
            # NEVER silently re-key: the relay only trusts the matching pubkey.
            raise PushIdentityError(
                f"Push identity key at {key_path} is unreadable ({err}). "
                "Restore it from backup, or delete the file to re-key — "
                "re-keying needs the hub re-registered with the relay."
            ) from err

    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    # 0600 from the first byte, never world-readable even briefly (O_EXCL so a
    # concurrent first boot can't race two keys into existence).
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    _LOGGER.info("Generated permanent Ed25519 push-identity key at %s", key_path)
    return private_key


def ensure_push_identity(
    data_dir: Path, hub_config: JsonConfigStore
) -> PushSigner:
    """Load-or-create the push-identity key (blocking — executor only).

    Mirrors the public key (hex) into ``hub_config`` whenever it is missing
    or out of date, so the value read by the relay-registration tooling
    always matches the private key actually used to sign. Raises
    ``PushIdentityError`` only for an unusable key file.
    """
    signer = PushSigner(_load_or_create_identity(data_dir / PUSH_IDENTITY_KEY_FILENAME))
    if hub_config.get(PUSH_PUBLIC_KEY_CONFIG_KEY) != signer.public_key_hex:
        hub_config.set(PUSH_PUBLIC_KEY_CONFIG_KEY, signer.public_key_hex)
        _LOGGER.info(
            "Push-identity public key published to hub_config (register with relay): %s",
            signer.public_key_hex,
        )
    return signer
