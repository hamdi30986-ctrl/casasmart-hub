# CasaSmart Hub

A Home Assistant integration that turns a Home Assistant installation into a
CasaSmart hub, the home server behind the CasaSmart phone and tablet apps. It
adds what the apps need:

- secure pairing for each person's phone;
- rooms, favorites and scenes;
- a hub-side security alarm;
- water-tank monitoring;
- speaker announcements and prayer-time athan;
- Energy Saving;
- push notifications through the CasaSmart relay;
- optional remote access through a Cloudflare tunnel.

## What you need

- **Home Assistant 2025.3 or newer**, installed through [HACS](https://hacs.xyz).
- **A push relay address and a hub activation code from CasaSmart.** Setup
  can't finish without them. Your installer gets both from the CasaSmart
  Installer Console: a relay URL such as `https://relay.example.com`, and a
  single-use `CSACT1…` code.
- **The CasaSmart app** on the phones and tablets that will use the hub.
- **A local network the phones share with the hub.** Phones reach the hub on
  **TCP port 8443** and find it through **mDNS** (`_casasmart._tcp`, UDP 5353).
  Pairing only works on that network.

## Install

1. In HACS, open the menu and choose **Custom repositories**. Add
   `https://github.com/hamdi30986-ctrl/casasmart-hub` with category
   **Integration**.
2. Download **CasaSmart Hub**. HACS offers releases only; branch installs are
   hidden on purpose.
3. Restart Home Assistant.

Update through HACS as well. HACS records which release it installed, so files
copied in by hand leave HACS showing the wrong version.

## Set up

1. Go to **Settings → Devices & services → Add integration → CasaSmart Hub**.
2. Enter the push relay URL and the activation code. Optionally enter the
   hub's Cloudflare tunnel hostname (see [Remote access](#remote-access)).
3. Two notifications appear in Home Assistant:
   - **pairing code**: the owner's one-time code, which claims the hub;
   - **recovery code**: the owner's recovery code. Write it down and keep it
     safe.
4. On a phone **on the same network**, open the CasaSmart app, choose the hub
   and enter the owner pairing code. That phone becomes the owner.
5. Invite everyone else from the app (Family → Invite member). Each invite is a
   single-use code that carries the person's role and rooms.

The activation code is used once to register the hub with the relay, then
deleted. To register again (for example after changing relays), open the
integration's **Configure** dialog and paste a fresh code.

## What it adds to Home Assistant

| Kind | Name | Purpose |
|---|---|---|
| Alarm panel | `alarm_control_panel.casasmart_hub_security` | The hub's alarm, armed and disarmed from the app or HA |
| Button | `button.casasmart_regenerate_pairing_code` | Unpair every phone, drop their push tokens, favorites and per-person settings, and issue a new owner code (the old printed code stops working) |
| Button | `button.casasmart_factory_reset` | Wipe the app layer (see below) and issue new owner and recovery codes |
| Sensor | `sensor.casasmart_energy_savings` | The active Energy Saving level: `off`, `low`, `medium` or `smart` |
| Sensors | `sensor.casasmart_user_*` | One per paired phone |
| Service | `casasmart.factory_reset` | The same reset as the button |
| Service | `casasmart.activate_scene` | Run a CasaSmart scene from an automation |
| Service | `casasmart.set_tunnel_url` | Set the tunnel address the hub gives to phones. A bare `https://host` also becomes the Cloudflare tunnel hostname, with the tunnel switched on unless it was switched off before (see [Remote access](#remote-access)). |
| Service | `casasmart.configure_hq_notifications` | Trust a signing key for HQ reminder notifications (Home Assistant admins only) |

**Automation events:**
- `casasmart_alarm_triggered`: an armed zone or a life-safety sensor tripped.
  Hook your siren here.
- `casasmart_tank_low` (checked daily at 18:00, Home Assistant's time) and
  `casasmart_tank_offline` (a tank silent for 20 minutes): at most once per
  tank per day, and only while push notifications are set up.
- `casasmart_auth_changed`: a phone was paired, recovered or unpaired, or its
  role or rooms changed.
- `casasmart_alarm_changed`, `casasmart_registry_changed`,
  `casasmart_audio_changed`, `casasmart_energy_changed`,
  `casasmart_tank_changed` and `casasmart_suggestions_changed`: state changes.

**Factory reset clears:**
- paired phones and pairing and recovery codes (both codes rotate);
- favorites, scenes, per-person settings, Now and suggestion data;
- push tokens, HQ notification data, the alarm log and armed state;
- audio and Energy Saving data;
- room tags, and the floors, rooms and device grouping, which re-seed from
  Home Assistant.

**It keeps** tanks, alarm zones and settings, the hub's identity, relay
registration, tunnel settings, and everything in Home Assistant itself.

**Handing the hub to a new owner:** use factory reset. "Regenerate pairing
code" keeps the recovery code, so whoever holds the old recovery card could
still take the hub back from the local network.

## Networking

The hub serves the apps on its own TLS port (8443). Home Assistant's own port
(8123) carries the Cloudflare tunnel and, on the LAN, readings from tank
sensors.

| Home Assistant install | What to do |
|---|---|
| Home Assistant OS / Supervised | Nothing. Port 8443 and mDNS work out of the box. |
| Container on Linux | Use `network_mode: host` (recommended for HA anyway). mDNS needs it; otherwise also publish `8443:8443`. |
| Docker Desktop (macOS) | Follow [deploy/macos](deploy/macos/README.md). Docker Desktop hides phones' addresses and can't announce mDNS, so two small helpers run on the Mac. |

### Pairing stays on the local network

Pairing a phone and owner recovery are only accepted from the hub's own
network. A speaker fetches its broker settings with the hub's provisioning
key (the `X-CasaSmart-Provision-Key` header, set to `provision_secret` from
`hub_config.json`), which works from any address. Speakers that don't send the
key are refused unless you turn on `keyless_speaker_provisioning`, and then
only from the local network.

- **Normally** the hub checks the client's address: private or link-local
  addresses count, loopback doesn't.
- **On Docker Desktop**, where addresses are hidden, you can tell it to trust
  its own TLS port behind the LAN-only relay instead (`lan_relay_ingress`,
  below). It never does this on its own.
- **Through Cloudflare**, requests are never local, so the owner claim and
  recovery can't go through a tunnel, and by default neither can any other
  pairing.

To let invited members pair from anywhere, set `remote_pairing_enabled`. The
owner claim always stays local.

### Remote access

Phones reach the hub from outside the home through a Cloudflare tunnel to Home
Assistant. Enter the tunnel's hostname during setup, which also turns the
tunnel on, or in **Configure** together with the **Cloudflare tunnel enabled**
switch. The hub gives the address to phones when they pair.

On Home Assistant OS and Supervised installs, install and set up the
Cloudflare Tunnel add-on first. With the switch on, the integration starts the
add-on and keeps it starting at boot; with it off, it stops the add-on and
sets it to manual start, so it stays down across reboots. Clearing the
hostname gives the add-on back its start at boot. While the tunnel is on, the
hub also checks it through Cloudflare every 5 minutes and restarts the add-on
if Cloudflare reports it disconnected.

On other installs, run cloudflared yourself and point it at Home Assistant;
the switch has no effect there.

## Hub settings

Advanced settings live in `/config/casasmart/hub_config.json`. Most hubs need
none of them.

**To change one:**
1. Stop Home Assistant. The hub keeps this file in memory and rewrites it, so
   edits made while it runs can be lost.
2. Edit the JSON. It must stay a valid JSON object: if the hub can't read it,
   the integration doesn't start, and Home Assistant retries until it is fixed.
3. Start Home Assistant.

| Key | Value | Effect |
|---|---|---|
| `hub_name` | string | Name shown when phones discover the hub (default "CasaSmart Hub"). On Docker Desktop the Mac helper's `--name` is shown instead. |
| `tls_port` | integer | The hub's TLS port (default `8443`). The apps expect 8443. A value that isn't a port number (1–65535) is ignored with a warning. |
| `lan_relay_ingress` | `"on"` / `"off"` | Whether the TLS port counts as local network (default `"off"`). See below. |
| `remote_pairing_enabled` | `true` / `false` | Let invited members pair from outside the network (default `false`) |
| `zigbee_base_topics` | list of strings | zigbee2mqtt base topics that "add a device" opens (default `["zigbee2mqtt"]`) |
| `tank_ingest_url` | full URL | Where tank sensors post readings, e.g. `http://192.168.1.20:8123/api/casasmart/tank/reading`. The default uses the hub's own LAN address and HA's port; set this when that address isn't reachable from the LAN (Docker Desktop, bridge networking). A tank keeps the address it was given at setup until it is set up again. |
| `keyless_speaker_provisioning` | `true` / `false` | Let speakers on the local network fetch their broker settings without the provisioning key (default `false`). Only for speakers that don't send the key; the hub logs a warning while it is on. |
| `update_repo` | `owner/repo` | Turns on the built-in updater for that GitHub repository. Off by default; update through HACS instead. The updater keeps the previous version in `/config/casasmart/update/rollback`; to roll back, stop Home Assistant and put that folder in place of `/config/custom_components/casasmart`. |

**`lan_relay_ingress` values:**
- `"off"` (the default) checks client addresses.
- `"on"` trusts every connection on the TLS port as local. It's meant for the
  [Docker Desktop setup](deploy/macos/README.md), where the port is published
  to `127.0.0.1` only and reached through the LAN-only relay. Never use it if
  the port is reachable any other way. That includes anything on the same
  machine that forwards outside traffic to it: Tailscale Serve or Funnel, ngrok,
  `ssh -R`, or a reverse-proxy container.

The hub logs a WARNING at startup whenever the TLS port is trusted. On Docker
Desktop with the setting unset, it logs a WARNING saying what to set instead.
There the address check is unreliable in both directions (Docker Desktop shows
a made-up address that changes between restarts), so what keeps the hub's
port off the internet is the loopback-only publish plus the relay, whatever
this setting says.

Don't edit the other keys in the file. They hold the hub's secrets, code hashes,
its push public key and values the services set.

## Data, backups and removal

The hub keeps its own data in `/config/casasmart/`:
- the database;
- its TLS and push identity keys;
- `hub_config.json`;
- automatic database backups before migrations;
- the built-in updater's work and rollback copy (`update/`), if it is used.

Home Assistant backups include it. Keep that folder intact when you move the
hub, or every phone will have to pair again.

The relay address, the tunnel hostname and switch, and the activation code
until it is used are in the integration's Home Assistant config entry.
Automations made in the app are ordinary Home Assistant automations, saved in
`automations.yaml`.

Removing the integration leaves `/config/casasmart/` in place, so reinstalling
keeps the pairings. Delete the folder (with Home Assistant stopped) to start
from scratch.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Pairing is only available on the hub's own network" | The phone must be on the hub's Wi-Fi/LAN, not mobile data, a VPN or the tunnel. On Docker Desktop, check the relay from [deploy/macos](deploy/macos/README.md). |
| "Too many failed attempts" | Five wrong codes in a row from one phone lock that phone out for a minute, and repeats for longer. Behind the Docker Desktop relay all phones share one lockout. |
| Phones can't find the hub | mDNS isn't reaching them: check host networking (Container) or the mDNS helper (Docker Desktop), and that the Wi-Fi doesn't isolate clients. |
| No push notifications | Look for a CasaSmart notification about the relay or activation in Home Assistant. Registration may need a fresh activation code (Configure). A relay that is only unreachable for a while is retried and logged, without a notification. |

For detail, enable debug logging:

```yaml
logger:
  logs:
    custom_components.casasmart: debug
```

## Development

The tests stub Home Assistant, so they run without it:

```sh
uv run --python 3.13 --no-project --with-requirements requirements_test.txt -- python -m pytest -q
uvx ruff@0.16.10 check . && uvx ruff@0.16.10 format --check .
```

The view-layer tests skip unless a real Home Assistant is importable. CI also
runs the suite against Home Assistant itself (`test-ha` in
`.github/workflows/ci.yml`). Contracts for some app features are in
[`docs/api/`](docs/api/).

## Releasing

Releases are cut only with `scripts/release.sh` (`check`, `tag`, `publish`,
`promote`). The script enforces the rules HACS depends on:

- Release from a clean checkout of `origin/main` with the tests passing.
- Use three-part versions. The tag `vX.Y.Z` must equal `manifest.json`'s
  `X.Y.Z`, and `CHANGELOG.md` must have a dated `## [X.Y.Z] - YYYY-MM-DD`
  section.
- `casasmart.zip` contains exactly the integration folder (files at the zip
  root). It is signed with the release key (`casasmart.zip.sig`).
- Publish first as a prerelease, verify on a hub, then promote it to latest.
- Never move or delete a published tag or asset; fix forward with a new version.

## License

Proprietary, not open source. You may install and use the hub, unmodified, on
a Home Assistant installation you own or administer, for use with CasaSmart
apps and services. Modifying or redistributing it needs written permission
from CasaSmart. See [LICENSE](LICENSE).
