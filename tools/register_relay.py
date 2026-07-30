#!/usr/bin/env python3
"""Register a hub's Ed25519 push identity with the CasaSmart push relay.

THE provisioning step that makes push notifications work. Every hub signs
its push batches with a permanent Ed25519 key generated on first boot
(``push_crypto.py``); the relay verifies that signature against a public
key it must already know. The hub NEVER self-registers (plan B8 — a hub
holding the relay admin token could register/rotate *any* hub's key), so
an operator runs this once per hub at provisioning. Until then every push
the hub sends dies at the relay with ``401 unknown_hub``.

What it reads from the hub's data dir (``<ha-config>/casasmart/``):

- ``identity_key.pem``          -> hub_id  = SHA-256 over the TLS identity
                                   SPKI DER, lowercase hex (the same id the
                                   dispatcher authenticates as — tls.py's
                                   ``identity_fingerprint``)
- ``push_identity_key.bin``     -> public_key = the Ed25519 key's public
                                   half, lowercase hex (64 chars), exactly
                                   what the relay's registry validates
- ``hub_config.json``           -> cross-check mirror ``push_public_key``
                                   + optional ``push_relay_url`` override

The admin token is supplied AT RUN TIME via ``--admin-token`` or the
``RELAY_ADMIN_TOKEN`` / ``ADMIN_TOKEN`` env vars. It is deliberately never
read from, or written to, any hub file — client hubs must not hold the
relay admin secret.

Usage (operator machine or hub host with the data dir mounted):

    RELAY_ADMIN_TOKEN=... python3 tools/register_relay.py \
        --data-dir /path/to/ha-config/casasmart

    # HAOS / Container installs: run inside the homeassistant container,
    # where python3 + cryptography are guaranteed:
    docker exec -e RELAY_ADMIN_TOKEN=... homeassistant \
        python3 /config/register_relay.py --data-dir /config/casasmart

    # No file access? Pass the two values directly (same as the relay
    # repo's scripts/register-hub.sh):
    RELAY_ADMIN_TOKEN=... python3 tools/register_relay.py \
        --hub-id <64-hex> --public-key <64-hex>

    # Inspect what would be sent, no token needed:
    python3 tools/register_relay.py --data-dir ... --dry-run

Idempotent: the relay's register endpoint is an upsert, so re-running is
always safe. Key rotation = delete ``push_identity_key.bin`` on the hub,
restart it (it re-keys and re-mirrors), then re-run this script.

Exit codes: 0 ok · 2 bad input/missing token/missing key files ·
3 relay unreachable · 4 admin token rejected · 5 relay rejected the request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# Keep in sync with custom_components/casasmart/const.py PUSH_RELAY_URL_DEFAULT
# (duplicated so this file works copied standalone onto a hub host;
# tests/test_register_relay.py pins the two values equal so they can't drift).
DEFAULT_RELAY_URL = "https://casasmart-relay.hamdi30986.deno.net"
REGISTER_PATH = "/admin/register-hub"

# File/key names, mirroring const.py / tls.py / push_crypto.py (same
# standalone-copy rationale; the drift test pins these too).
DATA_DIR_NAME = "casasmart"
IDENTITY_KEY_FILENAME = "identity_key.pem"
PUSH_IDENTITY_KEY_FILENAME = "push_identity_key.bin"
HUB_CONFIG_FILENAME = "hub_config.json"
PUSH_PUBLIC_KEY_CONFIG_KEY = "push_public_key"
PUSH_RELAY_URL_CONFIG_KEY = "push_relay_url"

HTTP_TIMEOUT_SECONDS = 15
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Exit codes (documented in the module docstring).
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_UNREACHABLE = 3
EXIT_TOKEN_REJECTED = 4
EXIT_RELAY_REJECTED = 5


class ProvisioningError(Exception):
    """A precondition failed — message is operator-facing, exit code attached."""

    def __init__(self, message: str, exit_code: int = EXIT_INPUT) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def resolve_data_dir(raw: str) -> Path:
    """Accept either ``<ha-config>`` or ``<ha-config>/casasmart``."""
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise ProvisioningError(f"--data-dir {path} is not a directory")
    # Operators pass the HA config root as often as the casasmart subdir;
    # accept both, preferring the dir that actually holds the identity key.
    if (path / IDENTITY_KEY_FILENAME).exists():
        return path
    nested = path / DATA_DIR_NAME
    if (nested / IDENTITY_KEY_FILENAME).exists():
        return nested
    raise ProvisioningError(
        f"No {IDENTITY_KEY_FILENAME} under {path} (or {nested}). "
        "Point --data-dir at the hub's casasmart data dir — the hub must "
        "have completed its first boot so the identity keys exist."
    )


def derive_hub_id(identity_pem: Path) -> str:
    """hub_id = SHA-256 over the TLS identity public key's SPKI DER, hex.

    Byte-for-byte the ``identity_fingerprint`` in tls.py — the id the push
    dispatcher authenticates as. Uses ``cryptography`` when available (always
    true inside the HA container), else falls back to the ``openssl`` binary.
    """
    if not identity_pem.exists():
        raise ProvisioningError(
            f"TLS identity key not found at {identity_pem} — the hub has not "
            "completed first boot yet. Start the hub once, then re-run."
        )
    try:
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(
            identity_pem.read_bytes(), password=None
        )
        spki = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except ImportError:
        spki = _spki_der_via_openssl(identity_pem)
    except ValueError as err:
        raise ProvisioningError(f"Could not parse {identity_pem}: {err}") from err
    return hashlib.sha256(spki).hexdigest()


def _spki_der_via_openssl(identity_pem: Path) -> bytes:
    """PEM private key -> SPKI DER using the openssl CLI (no-cryptography hosts)."""
    try:
        proc = subprocess.run(
            ["openssl", "pkey", "-in", str(identity_pem), "-pubout", "-outform", "DER"],
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as err:
        raise ProvisioningError(
            "Neither the python 'cryptography' package nor the 'openssl' binary "
            "is available — install either to derive the hub id."
        ) from err
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode(errors="replace").strip()
        raise ProvisioningError(f"openssl failed reading {identity_pem}: {detail}")
    return proc.stdout


def _load_hub_config(data_dir: Path) -> dict[str, Any]:
    """The hub_config.json contents, or {} when absent/unreadable-as-dict."""
    config_path = data_dir / HUB_CONFIG_FILENAME
    if not config_path.exists():
        return {}
    try:
        loaded = json.loads(config_path.read_text())
    except (OSError, ValueError) as err:
        raise ProvisioningError(f"Could not read {config_path}: {err}") from err
    return loaded if isinstance(loaded, dict) else {}


def read_push_public_key(data_dir: Path, hub_config: dict[str, Any]) -> str:
    """The hub's Ed25519 push public key, lowercase hex (64 chars).

    Ground truth is the private key file itself (derive the public half) —
    that is the key the dispatcher actually signs with. The hub_config mirror
    (written by ``ensure_push_identity`` every boot) is the fallback for hosts
    without ``cryptography``, and a cross-check when both are readable: a
    mismatch means the hub hasn't rebooted since the key file changed, and
    registering either value blind could pin the wrong key at the relay.
    """
    key_file = data_dir / PUSH_IDENTITY_KEY_FILENAME
    mirror = hub_config.get(PUSH_PUBLIC_KEY_CONFIG_KEY)
    mirror = mirror.lower() if isinstance(mirror, str) else None

    derived: Optional[str] = None
    if key_file.exists():
        raw = key_file.read_bytes()
        if len(raw) != 32:
            raise ProvisioningError(
                f"{key_file} is {len(raw)} bytes, expected 32 — the push key is "
                "corrupt. Restore it from backup or delete it and restart the hub."
            )
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            derived = (
                Ed25519PrivateKey.from_private_bytes(raw)
                .public_key()
                .public_bytes_raw()
                .hex()
            )
        except ImportError:
            derived = None  # fall back to the hub_config mirror below

    if derived is not None and mirror is not None and derived != mirror:
        raise ProvisioningError(
            "push_identity_key.bin and the hub_config push_public_key mirror "
            "disagree — restart the hub so it re-mirrors the key, then re-run."
        )
    public_key = derived or mirror
    if public_key is None:
        raise ProvisioningError(
            f"No push identity found: neither {key_file} (readable with the "
            f"'cryptography' package) nor a {PUSH_PUBLIC_KEY_CONFIG_KEY!r} entry "
            f"in {data_dir / HUB_CONFIG_FILENAME}. The hub generates both on "
            "first boot — start it once, then re-run."
        )
    if not _HEX64_RE.match(public_key):
        raise ProvisioningError(
            f"Push public key {public_key!r} is not 64 lowercase hex chars — "
            "refusing to register a malformed key."
        )
    return public_key


def resolve_relay_url(cli_value: Optional[str], hub_config: dict[str, Any]) -> str:
    """--relay beats the hub's configured ``push_relay_url`` beats the default.

    Honoring the hub_config override matters: registration must land on the
    SAME relay this hub will push to, including per-fleet repointed relays.
    """
    configured = hub_config.get(PUSH_RELAY_URL_CONFIG_KEY)
    url = cli_value or (configured if isinstance(configured, str) and configured.strip() else None) \
        or DEFAULT_RELAY_URL
    url = url.strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ProvisioningError(f"Relay URL {url!r} must be http(s)://")
    if url.startswith("http://"):
        print(
            f"WARNING: {url} is plain http — fine for a local test relay, "
            "never for production.",
            file=sys.stderr,
        )
    return url


def read_admin_token(cli_value: Optional[str]) -> str:
    """--admin-token, else RELAY_ADMIN_TOKEN / ADMIN_TOKEN env. Never stored."""
    token = (
        cli_value
        or os.environ.get("RELAY_ADMIN_TOKEN")
        or os.environ.get("ADMIN_TOKEN")
        or ""
    ).strip()
    if not token:
        raise ProvisioningError(
            "No admin token: pass --admin-token or set RELAY_ADMIN_TOKEN (or "
            "ADMIN_TOKEN). The token is the relay's admin bearer secret — it is "
            "supplied at run time and must never be stored on a client hub."
        )
    return token


def register(relay_url: str, hub_id: str, public_key: str, token: str) -> int:
    """POST the registration; print the outcome; return the exit code."""
    endpoint = relay_url + REGISTER_PATH
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"hub_id": hub_id, "public_key": public_key}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as err:  # non-2xx still carries a JSON body
        status, body = err.code, err.read()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        reason = getattr(err, "reason", err)
        print(f"ERROR: relay unreachable at {endpoint}: {reason}", file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}

    if status == 200:
        print(f"Registered hub {hub_id} with {relay_url}")
        print(f"  public_key    = {public_key}")
        print(f"  registered_at = {payload.get('registered_at', '?')}")
        print("Re-running is safe (register is an upsert); after a hub re-key, "
              "restart the hub and run this again.")
        return EXIT_OK
    if status == 401:
        print(
            "ERROR: the relay rejected the admin token (401). Check "
            "RELAY_ADMIN_TOKEN against the relay deployment's ADMIN_TOKEN.",
            file=sys.stderr,
        )
        return EXIT_TOKEN_REJECTED
    detail = payload.get("message") or repr(body[:200])
    print(
        f"ERROR: relay refused the registration (HTTP {status}): {detail}",
        file=sys.stderr,
    )
    return EXIT_RELAY_REJECTED


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register a hub's Ed25519 push identity with the CasaSmart relay."
    )
    parser.add_argument(
        "--data-dir",
        help="The hub's casasmart data dir (or the HA config dir containing it).",
    )
    parser.add_argument(
        "--hub-id",
        help="Override: the hub id (64-hex TLS identity fingerprint). "
        "With --public-key, no --data-dir is needed.",
    )
    parser.add_argument(
        "--public-key",
        help="Override: the Ed25519 push public key (64-hex).",
    )
    parser.add_argument(
        "--relay",
        help=f"Relay base URL (default: hub_config push_relay_url, else {DEFAULT_RELAY_URL}).",
    )
    parser.add_argument(
        "--admin-token",
        help="Relay admin bearer token (else RELAY_ADMIN_TOKEN / ADMIN_TOKEN env).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the hub_id / public_key / relay that WOULD be registered, then exit.",
    )
    args = parser.parse_args(argv)

    try:
        hub_config: dict[str, Any] = {}
        if args.hub_id and args.public_key:
            hub_id = args.hub_id.lower()
            public_key = args.public_key.lower()
            for label, value in (("--hub-id", hub_id), ("--public-key", public_key)):
                if not _HEX64_RE.match(value):
                    raise ProvisioningError(f"{label} must be 64 hex chars")
        elif args.data_dir:
            data_dir = resolve_data_dir(args.data_dir)
            hub_config = _load_hub_config(data_dir)
            hub_id = derive_hub_id(data_dir / IDENTITY_KEY_FILENAME)
            public_key = read_push_public_key(data_dir, hub_config)
        else:
            raise ProvisioningError(
                "Provide --data-dir, or both --hub-id and --public-key."
            )

        relay_url = resolve_relay_url(args.relay, hub_config)

        if args.dry_run:
            print(f"hub_id     = {hub_id}")
            print(f"public_key = {public_key}")
            print(f"relay      = {relay_url}{REGISTER_PATH}")
            return EXIT_OK

        token = read_admin_token(args.admin_token)
    except ProvisioningError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return err.exit_code

    return register(relay_url, hub_id, public_key, token)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
