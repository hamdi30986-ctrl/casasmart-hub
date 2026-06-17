"""Dev-only auto-enrollment seam — re-provisions trusted dev keys after a reset.

The problem it kills for good: the dev tooling (``.dev/mint_token.py`` and the
``b*_verify.py`` scripts) authenticates as a hub device whose id was, until now,
hand-injected into ``hub.db``. Every factory reset wiped that row, so every reset
meant another manual re-enrollment. This seam makes the dev identity survive any
reset automatically — no SQLite surgery, ever again.

How: on the dev hub a ``dev_devices.json`` manifest lists each trusted dev key.
The seam re-enrolls every entry as a **sub-admin** under a **fixed device id**
(:meth:`AuthEngine.ensure_enrolled`), so a hardcoded id never goes stale. It is
idempotent — already-correct entries are skipped — so it is safe to run on every
boot and after every reset.

INERT on client hubs by construction: it does nothing unless a manifest file is
present, and the manifest never ships in the integration (it lives in the hub's
data dir / the gitignored ``.dev/`` kit, never in the package). A production hub
simply has no manifest, so this module is a no-op there.

Wired in two places (see ``__init__.py``):

* once at integration **setup** — covers boot and the ``casasmart.factory_reset``
  service, which wipes then reloads the entry; and
* on **EVENT_AUTH_CHANGED** — covers the "Regenerate pairing code" BUTTON reset,
  which wipes the tables in place WITHOUT a reload, plus any accidental unpair.

Because :meth:`AuthEngine.ensure_enrolled` never fires ``EVENT_AUTH_CHANGED``,
the event listener can't feed itself — no loop.

No Home Assistant imports: pure storage/engine contracts, unit-testable on a
temp :class:`AuthEngine` exactly like the rest of the auth layer. Storage-touching
work is blocking — call :func:`ensure_dev_devices` via the executor, same rule as
the other engine methods.
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
    from .auth_tokens import ROLE_ADMIN, ROLE_SUB_ADMIN, VALID_ROLES
except ImportError:  # flat import in the test env (no HA package init)
    import auth_keys  # type: ignore[no-redef]
    from auth_engine import AuthEngine, EnrollError  # type: ignore[no-redef]
    from auth_tokens import (  # type: ignore[no-redef]
        ROLE_ADMIN,
        ROLE_SUB_ADMIN,
        VALID_ROLES,
    )

_LOGGER = logging.getLogger(__name__)

# The manifest file name. Searched in the hub DATA dir first (it survives a
# factory reset — reset wipes KV tables, not files) then the repo-side .dev kit.
DEV_DEVICES_FILENAME = "dev_devices.json"

# Default role for a provisioned dev device. Sub-admin sidesteps the
# single-admin invariant (the real owner / bootstrap admin code is untouched)
# and already carries automations.manage + cameras.view — exactly what the dev
# verify scripts exercise.
DEFAULT_DEV_ROLE = ROLE_SUB_ADMIN


def _candidate_paths(data_dir: Path) -> list[Path]:
    """Where a dev manifest may live, most-authoritative first.

    1. ``<data_dir>/dev_devices.json`` — the hub's own persistent data dir
       (``/config/casasmart`` in the container). Survives every factory reset.
    2. ``<repo_root>/.dev/dev_devices.json`` — the gitignored dev proof kit,
       resolved RELATIVE to this file so the same code finds it on the host
       repo AND inside the container (both keep ``custom_components/`` and
       ``.dev/`` as siblings under one root: the repo root on the host,
       ``/config`` in the container).
    """
    repo_root = Path(__file__).resolve().parents[2]
    return [
        data_dir / DEV_DEVICES_FILENAME,
        repo_root / ".dev" / DEV_DEVICES_FILENAME,
    ]


def _load_manifest(data_dir: Path) -> tuple[Path, list[dict[str, Any]]] | None:
    """First existing manifest as ``(path, entries)``, or None if there is none.

    A missing manifest is the normal client-hub case and returns None silently.
    A present-but-malformed manifest is logged and ALSO treated as absent — a
    typo in a dev-only file must never crash hub setup.
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
    """A stable ``dev-<hash>`` id derived from the canonical public key.

    Used only when a manifest entry omits an explicit ``device_id``, so the id
    is still fixed across resets (it is a pure function of the key) without
    forcing every entry to name one.
    """
    digest = hashlib.sha256(canonical_pem.encode()).hexdigest()
    return f"dev-{digest[:16]}"


def _normalize_entry(entry: Any) -> dict[str, Any] | None:
    """Validate + canonicalize one manifest entry, or None to skip it.

    Tolerant on field names — accepts the task's ``public_key`` / ``label`` and
    the design-doc's ``public_key_pem`` / ``name`` interchangeably — and
    fail-soft: any bad entry is logged and skipped, never fatal. NEVER yields an
    admin entry; the dev seam is sub-admin / user only by construction.
    """
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
    """Provision every device in the dev manifest; return the ids (re)written.

    BLOCKING (storage I/O) — call via the executor, like the rest of the auth
    engine's storage methods. Returns ``[]`` when no manifest exists, which is
    the case on every client hub (so this is a cheap no-op there). Idempotent:
    only ids whose stored record actually changed are returned, so a
    steady-state call writes nothing and reports nothing.
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
