"""Fleet provisioning registry — operator-side source of truth (Track B / B19 backbone).

This tool NEVER ships to a client hub. It is Hamdi's master list of every
CasaSmart hub deployed in the field, organized by geographic zone. It exists
to answer one question without guesswork as the fleet grows past a handful of
installs: *which client got which public subdomain, and how do I reach their
hub?*

Two roads hang off every record (decided 2026-06-14):

- **Cloudflare subdomain** — the client-facing road. Each hub gets a unique
  ``{subdomain}.{domain}`` that the client's APP dials over the internet. The
  registry hands these out and refuses collisions, so two hubs can never share
  an address.
- **Tailscale hostname** — the operator road. How the future master dashboard
  (B19) and manual SSH reach the hub privately. Optional at register time
  (the Tailscale node may not exist yet), settable later via ``update``.

Doctrine: **fail closed, never corrupt the fleet list.**

- Every write goes through an exclusive file lock (``fcntl``) + atomic
  temp-file rename, so a crash or two concurrent ``register`` runs can never
  leave a half-written or duplicated record.
- A subdomain or hub_id collision is a hard error, never a silent overwrite.
- Auto-allocation picks the lowest free slot in a zone's pool and reuses slots
  freed by ``decommission`` — the pool can't leak.

Pure stdlib so the unit tests import it flat, exactly like ``tunnel.py``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator

# A subdomain label per RFC 1123: lowercase alnum + hyphen, no leading/trailing
# hyphen, 1-63 chars. We never publish anything that isn't a clean DNS label.
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

STATUS_ACTIVE = "active"
STATUS_DECOMMISSIONED = "decommissioned"
_STATUSES = (STATUS_ACTIVE, STATUS_DECOMMISSIONED)


class RegistryError(Exception):
    """Operator-facing error (collision, unknown zone, bad input).

    Raised instead of mutating the fleet list — the CLI turns these into a
    clean message + non-zero exit, never a stack trace or a partial write.
    """


@dataclass
class HubRecord:
    """One deployed hub. Mirrors the launch-plan field list (line 338)."""

    hub_id: str
    client_name: str
    zone: str
    subdomain: str
    install_date: str
    ha_version: str = ""
    integration_version: str = ""
    tailscale_hostname: str = ""
    status: str = STATUS_ACTIVE

    def fqdn(self, domain: str) -> str:
        return f"{self.subdomain}.{domain}"


@dataclass
class _Zone:
    label: str
    prefix: str


@dataclass
class _Zones:
    domain: str
    zones: dict[str, _Zone] = field(default_factory=dict)


def load_zones(path: Path) -> _Zones:
    """Read the static zone config (north-jeddah → prefix etc.)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    domain = raw.get("domain")
    if not isinstance(domain, str) or not domain:
        raise RegistryError(f"zones config missing 'domain': {path}")
    zones: dict[str, _Zone] = {}
    for key, val in (raw.get("zones") or {}).items():
        zones[key] = _Zone(label=val.get("label", key), prefix=val["prefix"])
    if not zones:
        raise RegistryError(f"zones config defines no zones: {path}")
    return _Zones(domain=domain, zones=zones)


class FleetRegistry:
    """JSON-backed fleet store. One instance == one registry file.

    All mutating ops acquire an exclusive lock for the whole read-modify-write
    so concurrent CLI invocations serialize instead of clobbering each other.
    """

    def __init__(self, store_path: Path, zones: _Zones) -> None:
        self._path = store_path
        self._zones = zones
        self._lock_path = store_path.with_suffix(store_path.suffix + ".lock")

    # ---- locking + atomic persistence ----------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def _read(self) -> list[HubRecord]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [HubRecord(**row) for row in raw.get("hubs", [])]

    def _write(self, hubs: list[HubRecord]) -> None:
        payload = {"hubs": [asdict(h) for h in hubs]}
        body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
        # Atomic: write a sibling temp file, fsync, then rename over the target.
        # A rename is atomic on POSIX, so a reader never sees a partial file.
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".reg-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    # ---- read-only queries (no lock needed) ----------------------------

    def list(
        self, zone: str | None = None, status: str | None = None
    ) -> list[HubRecord]:
        hubs = self._read()
        if zone is not None:
            hubs = [h for h in hubs if h.zone == zone]
        if status is not None:
            hubs = [h for h in hubs if h.status == status]
        return sorted(hubs, key=lambda h: (h.zone, h.subdomain))

    def show(self, hub_id: str) -> HubRecord:
        for h in self._read():
            if h.hub_id == hub_id:
                return h
        raise RegistryError(f"no hub with id {hub_id!r}")

    @property
    def domain(self) -> str:
        return self._zones.domain

    # ---- subdomain allocation ------------------------------------------

    def _validate_zone(self, zone: str) -> _Zone:
        z = self._zones.zones.get(zone)
        if z is None:
            known = ", ".join(sorted(self._zones.zones))
            raise RegistryError(f"unknown zone {zone!r} (known: {known})")
        return z

    def _taken_subdomains(self, hubs: list[HubRecord]) -> set[str]:
        # Decommissioned hubs free their subdomain — that's the whole point of
        # the status field, so the pool can be reused.
        return {h.subdomain for h in hubs if h.status == STATUS_ACTIVE}

    def _allocate(self, zone: _Zone, taken: set[str]) -> str:
        """Lowest free ``{prefix}NN`` slot in the zone (njed01, njed02, ...)."""
        n = 1
        while True:
            candidate = f"{zone.prefix}{n:02d}"
            if candidate not in taken:
                return candidate
            n += 1

    @staticmethod
    def _validate_subdomain(value: str) -> str:
        label = value.strip().lower()
        if not _SUBDOMAIN_RE.match(label):
            raise RegistryError(
                f"invalid subdomain {value!r}: must be a DNS label "
                "(lowercase letters/digits/hyphens, no leading/trailing hyphen)"
            )
        return label

    # ---- mutations (locked, read-modify-write) -------------------------

    def register(
        self,
        hub_id: str,
        client_name: str,
        zone: str,
        *,
        subdomain: str | None = None,
        tailscale_hostname: str = "",
        ha_version: str = "",
        integration_version: str = "",
        install_date: str | None = None,
    ) -> HubRecord:
        """Add a hub, allocating (or claiming) a unique subdomain.

        ``subdomain=None`` auto-allocates the next free slot in the zone's
        pool. Passing one explicitly (e.g. ``noha``) claims that exact label
        after validating it's a clean DNS label and not already in use.
        """
        hub_id = hub_id.strip()
        if not hub_id:
            raise RegistryError("hub_id is required")
        if not client_name.strip():
            raise RegistryError("client_name is required")
        zone_def = self._validate_zone(zone)

        with self._locked():
            hubs = self._read()
            if any(h.hub_id == hub_id for h in hubs):
                raise RegistryError(f"hub_id {hub_id!r} already registered")

            taken = self._taken_subdomains(hubs)
            if subdomain is None:
                allocated = self._allocate(zone_def, taken)
            else:
                allocated = self._validate_subdomain(subdomain)
                if allocated in taken:
                    raise RegistryError(
                        f"subdomain {allocated!r} already in use by an active hub"
                    )

            record = HubRecord(
                hub_id=hub_id,
                client_name=client_name.strip(),
                zone=zone,
                subdomain=allocated,
                install_date=install_date or date.today().isoformat(),
                ha_version=ha_version.strip(),
                integration_version=integration_version.strip(),
                tailscale_hostname=tailscale_hostname.strip(),
                status=STATUS_ACTIVE,
            )
            hubs.append(record)
            self._write(hubs)
            return record

    def update(self, hub_id: str, **fields: str) -> HubRecord:
        """Mutate mutable fields of one hub (versions, tailscale host, etc.).

        Identity fields (hub_id, subdomain, zone) are intentionally NOT
        editable here — re-homing a hub is a decommission + re-register, so a
        subdomain can never be silently reassigned out from under the app.
        """
        allowed = {
            "client_name",
            "ha_version",
            "integration_version",
            "tailscale_hostname",
            "status",
        }
        bad = set(fields) - allowed
        if bad:
            raise RegistryError(
                f"cannot update {', '.join(sorted(bad))} "
                f"(editable: {', '.join(sorted(allowed))})"
            )
        if "status" in fields and fields["status"] not in _STATUSES:
            raise RegistryError(
                f"invalid status {fields['status']!r} (allowed: {', '.join(_STATUSES)})"
            )

        with self._locked():
            hubs = self._read()
            for i, h in enumerate(hubs):
                if h.hub_id == hub_id:
                    for key, val in fields.items():
                        setattr(h, key, val.strip() if isinstance(val, str) else val)
                    hubs[i] = h
                    self._write(hubs)
                    return h
            raise RegistryError(f"no hub with id {hub_id!r}")

    def decommission(self, hub_id: str) -> HubRecord:
        """Mark a hub retired and free its subdomain for reuse.

        We keep the row (audit trail of what was ever deployed) but flip status
        so the pool allocator and collision check treat the subdomain as free.
        """
        return self.update(hub_id, status=STATUS_DECOMMISSIONED)
