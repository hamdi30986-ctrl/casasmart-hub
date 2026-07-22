#!/usr/bin/env python3
"""Fleet registry CLI — the operator command surface.

Run from anywhere:

    python3 tools/fleet-registry/cli.py register \
        --hub-id hub-001 --client "Client A" --zone zone-north

    python3 tools/fleet-registry/cli.py list --zone zone-north
    python3 tools/fleet-registry/cli.py show hub-001
    python3 tools/fleet-registry/cli.py update hub-001 --ha-version 2026.6.1
    python3 tools/fleet-registry/cli.py decommission hub-001

The registry JSON lives at ``tools/fleet-registry/fleet.json`` by default
(git-ignored — it's operator data, not code). Override with --store.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from registry import (  # noqa: E402
    FleetRegistry,
    HubRecord,
    RegistryError,
    load_zones,
)

_HERE = Path(__file__).resolve().parent
_DEFAULT_STORE = _HERE / "fleet.json"
_DEFAULT_ZONES = _HERE / "config" / "zones.json"


def _fmt(record: HubRecord, domain: str) -> str:
    ts = record.tailscale_hostname or "—"
    return (
        f"  {record.hub_id:<18} {record.client_name:<16} "
        f"{record.zone:<15} {record.fqdn(domain):<28} "
        f"ts={ts:<20} ha={record.ha_version or '—':<11} "
        f"int={record.integration_version or '—':<9} [{record.status}]"
    )


def _build(args: argparse.Namespace) -> FleetRegistry:
    zones = load_zones(Path(args.zones))
    return FleetRegistry(Path(args.store), zones)


def _cmd_register(reg: FleetRegistry, args: argparse.Namespace) -> int:
    rec = reg.register(
        hub_id=args.hub_id,
        client_name=args.client,
        zone=args.zone,
        subdomain=args.subdomain,
        tailscale_hostname=args.tailscale or "",
        ha_version=args.ha_version or "",
        integration_version=args.integration_version or "",
    )
    fqdn = rec.fqdn(reg.domain)
    print(f"Registered {rec.hub_id} → {fqdn}")
    print()
    print("Bake into this hub's cloudflared ingress + hub_config.json:")
    print(f'  "tunnel_url": "https://{fqdn}"')
    return 0


def _cmd_list(reg: FleetRegistry, args: argparse.Namespace) -> int:
    hubs = reg.list(zone=args.zone, status=args.status)
    if not hubs:
        print("(no hubs match)")
        return 0
    for rec in hubs:
        print(_fmt(rec, reg.domain))
    print(f"\n{len(hubs)} hub(s)")
    return 0


def _cmd_show(reg: FleetRegistry, args: argparse.Namespace) -> int:
    rec = reg.show(args.hub_id)
    print(_fmt(rec, reg.domain))
    return 0


def _cmd_update(reg: FleetRegistry, args: argparse.Namespace) -> int:
    fields = {}
    if args.client is not None:
        fields["client_name"] = args.client
    if args.ha_version is not None:
        fields["ha_version"] = args.ha_version
    if args.integration_version is not None:
        fields["integration_version"] = args.integration_version
    if args.tailscale is not None:
        fields["tailscale_hostname"] = args.tailscale
    if not fields:
        raise RegistryError("update needs at least one field to change")
    rec = reg.update(args.hub_id, **fields)
    print(_fmt(rec, reg.domain))
    return 0


def _cmd_decommission(reg: FleetRegistry, args: argparse.Namespace) -> int:
    rec = reg.decommission(args.hub_id)
    print(f"Decommissioned {rec.hub_id} — subdomain {rec.subdomain!r} freed")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet-registry", description=__doc__)
    p.add_argument("--store", default=str(_DEFAULT_STORE), help="registry JSON path")
    p.add_argument("--zones", default=str(_DEFAULT_ZONES), help="zones config path")
    sub = p.add_subparsers(dest="cmd", required=True)

    reg = sub.add_parser("register", help="add a hub + allocate a subdomain")
    reg.add_argument("--hub-id", required=True)
    reg.add_argument("--client", required=True)
    reg.add_argument("--zone", required=True)
    reg.add_argument("--subdomain", default=None, help="explicit label (else auto)")
    reg.add_argument("--tailscale", default=None, help="tailscale hostname")
    reg.add_argument("--ha-version", default=None)
    reg.add_argument("--integration-version", default=None)
    reg.set_defaults(func=_cmd_register)

    lst = sub.add_parser("list", help="list hubs (filter by zone/status)")
    lst.add_argument("--zone", default=None)
    lst.add_argument("--status", default=None, choices=["active", "decommissioned"])
    lst.set_defaults(func=_cmd_list)

    shw = sub.add_parser("show", help="show one hub")
    shw.add_argument("hub_id")
    shw.set_defaults(func=_cmd_show)

    upd = sub.add_parser("update", help="change versions / tailscale / client name")
    upd.add_argument("hub_id")
    upd.add_argument("--client", default=None)
    upd.add_argument("--ha-version", default=None)
    upd.add_argument("--integration-version", default=None)
    upd.add_argument("--tailscale", default=None)
    upd.set_defaults(func=_cmd_update)

    dec = sub.add_parser("decommission", help="retire a hub, free its subdomain")
    dec.add_argument("hub_id")
    dec.set_defaults(func=_cmd_decommission)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reg = _build(args)
        return args.func(reg, args)
    except RegistryError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
