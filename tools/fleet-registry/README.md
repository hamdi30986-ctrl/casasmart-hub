# Fleet Provisioning Registry

**Operator-side tool. Never ships to a client hub.** This is Hamdi's master list
of every CasaSmart hub deployed in the field — the data backbone the future
master dashboard (B19) reads from.

## Why it exists

Every hub you install needs a **unique public address**:

- **Cloudflare subdomain** (`noha.casasmart.sa`) — the road the client's **app**
  takes over the internet.
- **Tailscale hostname** — the private road **you** take to SSH / run the master
  dashboard.

Without a registry you'd pick subdomains by hand and eventually collide. This
tool allocates them, refuses duplicates, and remembers everything per hub:
hub ID, client, zone, subdomain, install date, HA version, integration version,
Tailscale host, status.

## Usage

```bash
cd casasmart-hub

# Register a hub — auto-allocates the next free subdomain in the zone
python3 tools/fleet-registry/cli.py register \
    --hub-id omar-apt --client "Omar" --zone east-jeddah

# ...or claim an explicit subdomain
python3 tools/fleet-registry/cli.py register \
    --hub-id noha-villa --client "Noha" --zone north-jeddah \
    --subdomain noha --tailscale noha-hub

python3 tools/fleet-registry/cli.py list                 # whole fleet
python3 tools/fleet-registry/cli.py list --zone riyadh
python3 tools/fleet-registry/cli.py show noha-villa
python3 tools/fleet-registry/cli.py update noha-villa --ha-version 2026.6.1
python3 tools/fleet-registry/cli.py decommission omar-apt   # frees the subdomain
```

`register` prints the `tunnel_url` line to bake into that hub's `cloudflared`
ingress + `hub_config.json` (the B7 contract the app pins at pairing).

## Layout

```
tools/fleet-registry/
├── config/zones.json   # zone definitions + subdomain prefixes (edit to add zones)
├── src/registry.py     # core store: allocation, locking, atomic writes
├── cli.py              # operator command surface
├── tests/test_registry.py
└── fleet.json          # the live registry (git-ignored — operator data, not code)
```

## Design notes

- **Fail closed:** subdomain/hub_id collisions are hard errors, never silent
  overwrites. Subdomains are validated as clean DNS labels.
- **Atomic + locked:** every write takes an exclusive file lock and renames a
  temp file into place, so two concurrent runs can't corrupt the list.
- **Decommission, don't delete:** retiring a hub keeps the audit row but frees
  its subdomain for reuse.
- **Identity is immutable:** `update` can't change hub_id / subdomain / zone —
  re-homing a hub is decommission + re-register, so an address can never be
  silently reassigned out from under a live app.

## Tests

```bash
python3 -m unittest discover -s tools/fleet-registry/tests
```
