"""Developer seam: keep trusted test devices enrolled across resets.

For development hubs whose tooling signs in as a hub device. Off unless the
CASASMART_DEV_ENROLL environment variable is set and a dev_devices.json
manifest exists in the hub's data dir or in a .dev/ folder next to
custom_components/; the manifest never ships with the integration. Each entry
is a public key with an optional device id, label, role (sub-admin or user,
never admin) and rooms (users only), enrolled under a fixed id. Bad entries
are logged and skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

try:
    from . import auth_keys
    from .auth_engine import AuthEngine, EnrollError
    from .auth_tokens import ROLE_ADMIN, ROLE_SUB_ADMIN, ROLE_USER, VALID_ROLES
except ImportError:  # flat import in the test env (no HA package init)
    import auth_keys  # type: ignore[no-redef]
    from auth_engine import AuthEngine, EnrollError  # type: ignore[no-redef]
    from auth_tokens import (  # type: ignore[no-redef]
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        ROLE_USER,
        VALID_ROLES,
    )

_LOGGER = logging.getLogger(__name__)

# Looked up in the hub's data dir first, which a factory reset leaves alone,
# then in the .dev kit of a development checkout.
DEV_DEVICES_FILENAME = "dev_devices.json"

# Sub-admin leaves the owner and the single-admin rule alone, and already
# holds the permissions test tooling usually needs.
DEFAULT_DEV_ROLE = ROLE_SUB_ADMIN


def _candidate_paths(data_dir: Path) -> list[Path]:
    """Manifest locations, in lookup order.

    The .dev path is resolved from this file, so it works in a checkout and in
    the container, where custom_components/ and .dev/ share the /config root.
    """
    repo_root = Path(__file__).resolve().parents[2]
    return [
        data_dir / DEV_DEVICES_FILENAME,
        repo_root / ".dev" / DEV_DEVICES_FILENAME,
    ]


def _load_manifest(data_dir: Path) -> tuple[Path, list[dict[str, Any]]] | None:
    """The first manifest found as (path, entries), or None.

    A missing manifest is the normal case. A malformed one is logged and
    treated as missing, so a typo can't break hub setup.
    """
    for path in _candidate_paths(data_dir):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError) as err:
            _LOGGER.error("Dev enroll: %s is unreadable / not JSON: %s", path, err)
            return None
        if not isinstance(raw, list):
            _LOGGER.error("Dev enroll: %s must be a JSON array of entries", path)
            return None
        return path, raw
    return None


def _deterministic_device_id(canonical_pem: str) -> str:
    """A stable dev-<hash> id from the public key, for entries without a device_id."""
    digest = hashlib.sha256(canonical_pem.encode()).hexdigest()
    return f"dev-{digest[:16]}"


def _normalize_entry(entry: Any) -> dict[str, Any] | None:
    """Validate one manifest entry into enroll arguments, or None to skip it.

    public_key_pem and name are accepted for public_key and label. Bad
    entries, admin ones included, are logged and skipped.
    """
    if not isinstance(entry, dict):
        _LOGGER.error(
            "Dev enroll: manifest entry is not an object — skipped: %r", entry
        )
        return None

    public_key = entry.get("public_key") or entry.get("public_key_pem")
    if not isinstance(public_key, str) or not public_key.strip():
        _LOGGER.error("Dev enroll: entry missing public_key — skipped: %r", entry)
        return None
    try:
        canonical_pem = auth_keys.validate_public_key(public_key)
    except auth_keys.KeyError_ as err:
        _LOGGER.error("Dev enroll: entry has an invalid public key (%s) — skipped", err)
        return None

    role = entry.get("role") or DEFAULT_DEV_ROLE
    if role == ROLE_ADMIN:
        _LOGGER.error("Dev enroll: refusing to provision an admin device — skipped")
        return None
    if role not in VALID_ROLES:
        _LOGGER.error("Dev enroll: entry has unknown role %r — skipped", role)
        return None

    label = entry.get("label") or entry.get("name") or "dev device"
    device_id = entry.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        device_id = _deterministic_device_id(canonical_pem)

    rooms = entry.get("rooms")
    if rooms is not None and (
        not isinstance(rooms, list)
        or any(not isinstance(room, str) or not room for room in rooms)
    ):
        _LOGGER.error(
            "Dev enroll: entry %s has a malformed rooms list — skipped", device_id
        )
        return None
    if rooms is not None and role != ROLE_USER:
        # Only a user can be room-scoped; sub-admins see every room.
        _LOGGER.error(
            "Dev enroll: entry %s gives a %s a room scope, which only a user "
            "can have — skipped",
            device_id,
            role,
        )
        return None

    return {
        "device_id": device_id.strip(),
        "name": str(label),
        "role": role,
        "public_key_pem": canonical_pem,
        "rooms": rooms,
    }


def ensure_dev_devices(data_dir: Path, auth: AuthEngine) -> list[str]:
    """Enroll the manifest's devices; return the ids whose records changed.

    Blocking storage I/O: run it in the executor. Without a manifest it
    returns [] at once, and a repeat call writes nothing.
    """
    loaded = _load_manifest(data_dir)
    if loaded is None:
        return []
    path, entries = loaded

    changed: list[str] = []
    for entry in entries:
        normalized = _normalize_entry(entry)
        if normalized is None:
            continue
        try:
            wrote = auth.ensure_enrolled(
                device_id=normalized["device_id"],
                name=normalized["name"],
                role=normalized["role"],
                public_key_pem=normalized["public_key_pem"],
                rooms=normalized["rooms"],
            )
        except EnrollError as err:
            # One bad entry must not stop the others from being provisioned.
            _LOGGER.error(
                "Dev enroll: %s could not be provisioned: %s",
                normalized["device_id"],
                err,
            )
            continue
        if wrote:
            changed.append(normalized["device_id"])

    if changed:
        _LOGGER.warning(
            "Dev enroll: (re)provisioned %d dev device(s) from %s: %s",
            len(changed),
            path,
            ", ".join(changed),
        )
    return changed
