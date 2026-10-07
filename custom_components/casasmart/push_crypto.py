"""The hub's Ed25519 push-identity key, which signs every batch sent to the relay.

The key is generated on first boot and stays in push_identity_key.bin (0600);
its public key is mirrored into hub_config.json for operators. It is separate
from the P-256 TLS identity in tls.py, which pins the LAN certificate. A
corrupt key file is a hard error and is not replaced: a new key would not
match the one registered with the relay.
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

# The raw 32-byte private key, next to the TLS identity in <config>/casasmart/.
PUSH_IDENTITY_KEY_FILENAME = "push_identity_key.bin"
# For operators only (64 hex chars); registration reads the key from the signer.
PUSH_PUBLIC_KEY_CONFIG_KEY = "push_public_key"

_ED25519_KEY_BYTES = 32


class PushIdentityError(Exception):
    """The push-identity key file is unusable and needs a person to fix it."""


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
        """Return the raw 64-byte Ed25519 signature over message."""
        return self._private_key.sign(message)


def _load_or_create_identity(key_path: Path) -> Ed25519PrivateKey:
    """Load the raw key file, or mint and save one at 0600 on first boot.

    Raises PushIdentityError for a file of the wrong size or content.
    """
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
            # Don't replace it: the relay trusts only the registered public key.
            raise PushIdentityError(
                f"Push identity key at {key_path} is unreadable ({err}). "
                "Restore it from backup, or delete the file to re-key — "
                "re-keying needs the hub re-registered with the relay."
            ) from err

    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    # 0600 from creation; O_EXCL stops two concurrent first boots making two keys.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    _LOGGER.info("Generated permanent Ed25519 push-identity key at %s", key_path)
    return private_key


def ensure_push_identity(data_dir: Path, hub_config: JsonConfigStore) -> PushSigner:
    """Load or create the push-identity key. Blocking: run it in the executor.

    Also keeps push_public_key in hub_config in step with the key. Raises
    PushIdentityError for an unusable key file.
    """
    signer = PushSigner(_load_or_create_identity(data_dir / PUSH_IDENTITY_KEY_FILENAME))
    if hub_config.get(PUSH_PUBLIC_KEY_CONFIG_KEY) != signer.public_key_hex:
        hub_config.set(PUSH_PUBLIC_KEY_CONFIG_KEY, signer.public_key_hex)
        _LOGGER.info(
            "Push-identity public key written to hub_config: %s",
            signer.public_key_hex,
        )
    return signer
