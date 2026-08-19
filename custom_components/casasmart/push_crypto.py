"""CasaSmart runtime component."""

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



PUSH_IDENTITY_KEY_FILENAME = "push_identity_key.bin"


PUSH_PUBLIC_KEY_CONFIG_KEY = "push_public_key"


_ED25519_KEY_BYTES = 32


class PushIdentityError(Exception):
    """CasaSmart runtime component."""


class PushSigner:
    """CasaSmart runtime component."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self._public_key_hex = private_key.public_key().public_bytes_raw().hex()

    @property
    def public_key_hex(self) -> str:
        """CasaSmart runtime component."""
        return self._public_key_hex

    def sign(self, message: bytes) -> bytes:
        """CasaSmart runtime component."""
        return self._private_key.sign(message)


def _load_or_create_identity(key_path: Path) -> Ed25519PrivateKey:
    """CasaSmart runtime component."""
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

            raise PushIdentityError(
                f"Push identity key at {key_path} is unreadable ({err}). "
                "Restore it from backup, or delete the file to re-key — "
                "re-keying needs the hub re-registered with the relay."
            ) from err

    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()


    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    _LOGGER.info("Generated permanent Ed25519 push-identity key at %s", key_path)
    return private_key


def ensure_push_identity(
    data_dir: Path, hub_config: JsonConfigStore
) -> PushSigner:
    """CasaSmart runtime component."""
    signer = PushSigner(_load_or_create_identity(data_dir / PUSH_IDENTITY_KEY_FILENAME))
    if hub_config.get(PUSH_PUBLIC_KEY_CONFIG_KEY) != signer.public_key_hex:
        hub_config.set(PUSH_PUBLIC_KEY_CONFIG_KEY, signer.public_key_hex)
        _LOGGER.info(
            "Push-identity public key published to hub_config (register with relay): %s",
            signer.public_key_hex,
        )
    return signer
