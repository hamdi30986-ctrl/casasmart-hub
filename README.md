# CasaSmart Hub

Home Assistant integration for CasaSmart systems.

The hub TLS endpoint must be reachable from client devices on port `8443`, and
`_casasmart._tcp` must be advertised on the LAN. Native Home Assistant hosts do
this through the integration. Docker Desktop for macOS requires the companion
[LAN pairing bridge](deploy/macos/README.md) because its port publishing and
multicast behavior hide the client network from the container.

## Install

Requires Home Assistant **2025.3** or newer.

1. In HACS, add this repository as a custom repository (category *Integration*).
2. Download **CasaSmart Hub**, choosing a release (never a branch).
3. Restart Home Assistant, then add the *CasaSmart Hub* integration. Setup asks
   for the push relay URL and its activation code, and optionally a Cloudflare
   tunnel domain.

Update through HACS. HACS records the release tag it installed, so copying files
in by hand or using the hub's own updater leaves HACS showing the wrong version.

Pairing, owner recovery and keyless speaker provisioning are LAN-only. The hub
decides "LAN" from the client's address, except on Docker Desktop, where it
trusts its loopback-published TLS port behind the LAN relay instead (see the
bridge README; `lan_relay_ingress` overrides). Requests through Cloudflare are
never LAN. Remote pairing of member codes is opt-in (`remote_pairing_enabled`).

## Development

Tests stub Home Assistant, so they run without it installed:

```sh
uv run --python 3.13 --no-project --with-requirements requirements_test.txt -- python -m pytest -q
uvx ruff check . && uvx ruff format --check .
```

About 150 view-layer tests skip unless a real Home Assistant is importable (see
`tests/conftest.py`). API contracts for the app live in [`docs/api/`](docs/api/).

## Releasing

Releases are cut only with `scripts/release.sh` (`check`, `tag`, `publish`,
`promote`). The script enforces the rules HACS depends on:

- Use three-part versions. The tag `vX.Y.Z` must equal `manifest.json`'s
  `X.Y.Z`, and `CHANGELOG.md` must have a `## [X.Y.Z]` section.
- `casasmart.zip` is built from the tag with
  `git archive vX.Y.Z:custom_components/casasmart`, with files at the zip root.
  It is signed with the release key (`casasmart.zip.sig`).
- Publish first as a prerelease, verify on a hub, then promote it to latest.
- Never move or delete a published tag or asset; fix forward with a new version.

Proprietary software. © CasaSmart. All rights reserved.
