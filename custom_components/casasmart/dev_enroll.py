"""CasaSmart runtime component."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

try:
    from . import auth_keys
    from .auth_engine import AuthEngine, EnrollError
    from .auth_tokens import ROLE_ADMIN, ROLE_SUB_ADMIN, VALID_ROLES
except ImportError:
    import auth_keys
    from auth_engine import AuthEngine, EnrollError
    from auth_tokens import (
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        VALID_ROLES,
    )

_LOGGER = logging.getLogger(__name__)



DEV_DEVICES_FILENAME = "dev_devices.json"





DEFAULT_DEV_ROLE = ROLE_SUB_ADMIN


def _candidate_paths(data_dir: Path) -> list[Path]:
    """CasaSmart runtime component."""
    repo_root = Path(__file__).resolve().parents[2]
    return [
        data_dir / DEV_DEVICES_FILENAME,
        repo_root / ".dev" / DEV_DEVICES_FILENAME,
    ]


def _load_manifest(data_dir: Path) -> tuple[Path, list[dict[str, Any]]] | None:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
    digest = hashlib.sha256(canonical_pem.encode()).hexdigest()
    return f"dev-{digest[:16]}"


def _normalize_entry(entry: Any) -> dict[str, Any] | None:
    """CasaSmart runtime component."""
    if not isinstance(entry, dict):
        _LOGGER.error("Dev enroll: manifest entry is not an object — skipped: %r", entry)
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

    return {
        "device_id": device_id.strip(),
        "name": str(label),
        "role": role,
        "public_key_pem": canonical_pem,
        "rooms": rooms,
    }


def ensure_dev_devices(data_dir: Path, auth: AuthEngine) -> list[str]:
    """CasaSmart runtime component."""
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
