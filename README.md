# CasaSmart Hub

A Home Assistant integration that turns a Home Assistant installation into a
CasaSmart hub, the home server behind the CasaSmart phone and tablet apps. It
adds:

- secure pairing for each person's phone;
- rooms, favorites and scenes;
- a security alarm that runs on the hub;
- water-tank monitoring;
- speaker announcements and prayer-time athan;
- Energy Saving;
- push notifications through the CasaSmart relay;
- optional remote access through a Cloudflare tunnel.

## Before you start

You need:

- **Home Assistant 2025.3 or newer** with [HACS](https://hacs.xyz).
- **A push relay URL and a hub activation code.** Your installer gets both
  from the CasaSmart Installer Console: a URL such as
  `https://relay.example.com` and a single-use `CSACT1…` code. Setup can't
  finish without them.
- **The CasaSmart app** on each phone or tablet that will use the hub.
- **A local network the phones share with the hub.** Phones reach the hub on
  TCP port 8443 and find it with mDNS (`_casasmart._tcp`, UDP 5353). Pairing
  only works on this network.

Check that your install lets phones reach port 8443 and see mDNS:

| Home Assistant install | What to do |
|---|---|
| Home Assistant OS or Supervised | Nothing. |
| Container on Linux | Use `network_mode: host`, which mDNS needs (and Home Assistant recommends anyway). Otherwise, also publish `8443:8443`. |
| Docker Desktop on macOS | Follow the [Docker Desktop guide](deploy/macos/README.md). Docker Desktop hides phones' addresses and can't announce mDNS, so two small helpers run on the Mac. |

Home Assistant's own port, 8123, carries the Cloudflare tunnel and readings
from tank sensors on the LAN.

## Install

1. In HACS, open the menu and choose **Custom repositories**. Add
   `https://github.com/hamdi30986-ctrl/casasmart-hub` with the category
   **Integration**.
2. Download **CasaSmart Hub**. HACS offers releases only, not branches.
3. Restart Home Assistant.

## Set up

1. Go to **Settings → Devices & services → Add integration → CasaSmart Hub**.
2. Enter the push relay URL and the activation code. You can also enter the
   hub's Cloudflare tunnel hostname here (see [Remote access](#remote-access)).
3. Home Assistant shows two notifications:
   - the **pairing code** (the owner code printed on the hub), which claims
     it. It stops working once an owner has paired and works again if the
     owner removes the hub from the app, so keep the sticker;
   - the **recovery code**, which lets the owner take the hub back from a new
     phone. Write it down and keep it safe.
4. On a phone on the same network, open the CasaSmart app, choose the hub and
   enter the pairing code. That phone becomes the owner's.
5. Invite everyone else from the app (**Family → Invite member**). Each invite
   is a single-use code that carries the person's role and rooms.

The hub uses the activation code once to register with the relay, then
deletes it. To register again, for example after changing relays, open the
integration's **Configure** dialog and paste a fresh code.

## Remote access

Phones reach the hub from outside the home through a Cloudflare tunnel to
Home Assistant. Enter the tunnel's hostname during setup, which also switches
the tunnel on, or later in **Configure** together with the **Cloudflare
tunnel enabled** switch. Phones receive the address when they pair.

On Home Assistant OS and Supervised installs, install and set up the
Cloudflare Tunnel add-on first. The integration then manages it:

- **Switch on:** the add-on is started and set to start at boot.
- **Switch off:** the add-on is stopped and set to manual start, so it stays
  down after a reboot.
- **Hostname cleared:** the add-on is set to start at boot again.

While the tunnel is on, the hub checks it through Cloudflare every 5 minutes
and restarts the add-on if Cloudflare reports it disconnected.

On other installs, run cloudflared yourself and point it at Home Assistant.
The switch has no effect there.

## Pairing and the local network

Pairing a phone and owner recovery are only accepted from the hub's own
network:

- **Normally** the hub checks the client's address: private and link-local
  addresses count as local, loopback doesn't.
- **On Docker Desktop**, where client addresses are hidden, you can tell the
  hub to trust its TLS port behind the LAN-only relay instead
  (`lan_relay_ingress`, see [Hub settings](#hub-settings)). It never does this
  on its own.
- **Through Cloudflare**, requests never count as local, so the owner claim
  and recovery can't go through the tunnel, and by default neither can any
  other pairing.

To let invited members pair from anywhere, set `remote_pairing_enabled`. The
owner claim always stays local.

Speakers fetch their broker settings with the hub's provisioning key (the
`X-CasaSmart-Provision-Key` header, set to `provision_secret` from
`hub_config.json`), which works from any address. Speakers that don't send
the key are refused unless you turn on `keyless_speaker_provisioning`, and
then only from the local network.

## Updating

Update through HACS, which records the release it installed; files copied in
by hand leave it showing the wrong version. The built-in updater
(`update_repo` in [Hub settings](#hub-settings)) is off by default.

## What the hub adds to Home Assistant

| Kind | Name | Purpose |
|---|---|---|
| Alarm panel | `alarm_control_panel.casasmart_hub_security` | The hub's alarm, armed and disarmed from the app or Home Assistant |
| Button | `button.casasmart_regenerate_pairing_code` | Unpairs every phone, drops their push tokens, favorites and per-person settings, and issues a new owner code (the old printed code stops working). Home Assistant admins only |
| Button | `button.casasmart_factory_reset` | Wipes the app layer (see [Factory reset](#factory-reset)) and issues new owner and recovery codes. Home Assistant admins only |
| Sensor | `sensor.casasmart_energy_savings` | The active Energy Saving level: `off`, `low`, `medium` or `smart` |
| Sensors | `sensor.casasmart_user_*` | One per paired phone |
| Service | `casasmart.factory_reset` | The same reset as the button (Home Assistant admins only) |
| Service | `casasmart.activate_scene` | Runs a CasaSmart scene from an automation |
| Service | `casasmart.set_tunnel_url` | Sets the tunnel address the hub gives phones (Home Assistant admins only). A bare `https://host` also becomes the Cloudflare tunnel hostname, and switches the tunnel on unless it was switched off before (see [Remote access](#remote-access)). |
| Service | `casasmart.configure_hq_notifications` | Trusts a signing key for HQ reminder notifications (Home Assistant admins only) |

### Automation events

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

### Factory reset

A factory reset clears:

- paired phones, and the pairing and recovery codes (both are replaced);
- favorites, scenes, per-person settings, Now and suggestion data;
- push tokens, HQ notification data, the alarm log and armed state;
- audio and Energy Saving data;
- room tags, and the floors, rooms and device grouping, which are rebuilt
  from Home Assistant.

It keeps tanks, alarm zones and settings, the hub's identity, the relay
registration, tunnel settings, and everything in Home Assistant itself.

The hub's data is wiped in one step, and every phone's connection is closed.
If Energy Saving can't switch back on the automations it turned off, the
reset stops before wiping anything and says which ones. If it reports that it
couldn't finish, run it again; if it reports that the hub did not reload,
restart Home Assistant (every phone is already unpaired).

To hand the hub to a new owner, use factory reset. "Regenerate pairing code"
keeps the recovery code, so whoever holds the old recovery card could still
take the hub back from the local network.

## Hub settings

Advanced settings live in `/config/casasmart/hub_config.json`. Most hubs need
none of them. To change one:

1. Stop Home Assistant. The hub keeps this file in memory and rewrites it, so
   edits made while it runs can be lost.
2. Edit the JSON. It must stay a valid JSON object: if the hub can't read it,
   the integration doesn't start, and Home Assistant retries until it's fixed.
3. Start Home Assistant.

| Key | Value | Effect |
|---|---|---|
| `hub_name` | string | Name phones see when they discover the hub (default "CasaSmart Hub"). Only phones on the local network get it, through mDNS or the handshake; on Docker Desktop, mDNS shows the Mac helper's `--name` instead. |
| `tls_port` | integer | The hub's TLS port (default `8443`). The apps expect 8443. A value that isn't a port number (1–65535) is ignored with a warning. |
| `lan_relay_ingress` | `"on"` / `"off"` | Whether the TLS port counts as the local network (default `"off"`). See below. |
| `remote_pairing_enabled` | `true` / `false` | Lets invited members pair from outside the network (default `false`). |
| `zigbee_base_topics` | list of strings | zigbee2mqtt base topics that "add a device" opens (default `["zigbee2mqtt"]`). |
| `tank_ingest_url` | full URL | Where tank sensors post readings, such as `http://192.168.1.20:8123/api/casasmart/tank/reading`. The default uses the hub's own LAN address and Home Assistant's port; set this when the LAN can't reach that address (Docker Desktop, bridge networking). A tank keeps the address it was given until it is set up again. |
| `keyless_speaker_provisioning` | `true` / `false` | Lets speakers on the local network fetch their broker settings without the provisioning key (default `false`). Only for speakers that don't send the key; the hub logs a warning while it is on. |
| `update_repo` | `owner/repo` | Turns on the built-in updater for that GitHub repository (off by default). It keeps the previous version in `/config/casasmart/update/rollback`; to roll back, stop Home Assistant and put that folder in place of `/config/custom_components/casasmart`. |

### `lan_relay_ingress`

- `"off"` (the default): the hub checks client addresses.
- `"on"`: every connection on the TLS port counts as local. It's meant for the
  [Docker Desktop setup](deploy/macos/README.md), where the port is published
  to `127.0.0.1` only and reached through the LAN-only relay. Don't use it if
  the port can be reached any other way, including from anything on the same
  machine that forwards outside traffic to it: Tailscale Serve or Funnel,
  ngrok, `ssh -R` or a reverse-proxy container.

The hub logs a warning at startup whenever it trusts the TLS port. On Docker
Desktop with the setting unset, it logs a warning saying what to set instead.
There the address check is unreliable either way (Docker Desktop shows a
made-up address that changes between restarts), so what keeps the hub's port
off the internet is the loopback-only publish and the relay, whatever this
setting says.

Leave the other keys in the file alone. They hold the hub's secrets, code
hashes, its push public key and values the services set.

## Data, backups and removal

The hub keeps its own data in `/config/casasmart/`:

- the database;
- its TLS and push identity keys;
- `hub_config.json`;
- automatic database backups made before migrations;
- the built-in updater's work and rollback copy (`update/`), if it is used.

Home Assistant backups include this folder. Keep it intact when you move the
hub, or every phone will have to pair again.

The relay address, the tunnel hostname and switch, and the unused activation
code are kept in the integration's config entry. Automations made in the app
are ordinary Home Assistant automations, saved in `automations.yaml`.

Removing the integration leaves `/config/casasmart/` in place, so reinstalling
keeps the pairings. To start from scratch, stop Home Assistant and delete the
folder.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Pairing is only available on the hub's own network" | The phone must be on the hub's Wi-Fi or LAN, not mobile data, a VPN or the tunnel. On Docker Desktop, check the relay from the [Docker Desktop guide](deploy/macos/README.md). |
| "Too many failed attempts" | Five wrong codes in a row from one phone lock that phone out for a minute, and longer if it happens again. Behind the Docker Desktop relay all phones share one lockout. |
| Phones can't find the hub | mDNS isn't reaching them. Check host networking (Container) or the mDNS helper (Docker Desktop), and that the Wi-Fi doesn't isolate clients. |
| No push notifications | Look for a CasaSmart notification in Home Assistant about the relay or activation. Registration may need a fresh activation code (**Configure**). A relay that is only briefly unreachable is retried and logged, without a notification. If the log says push was skipped because the push-identity key couldn't be loaded or saved, check that `/config/casasmart/` is writable and not full. |

For more detail, turn on debug logging:

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

The view-layer tests need a real Home Assistant and are skipped without one;
CI runs them (`test-ha` in `.github/workflows/ci.yml`). API contracts for some
app features are in [`docs/api/`](docs/api/).

### Releasing

Releases are cut only with `scripts/release.sh` (`check`, `tag`, `publish`,
`promote`), which enforces what HACS depends on:

- a clean checkout of `origin/main`, with the tests passing;
- three-part versions: the tag `vX.Y.Z` matches `manifest.json`'s `X.Y.Z`,
  and `CHANGELOG.md` has a dated `## [X.Y.Z] - YYYY-MM-DD` section;
- `casasmart.zip` holds only the integration folder, with its files at the
  zip root, and is signed with the release key (`casasmart.zip.sig`);
- a release is published as a prerelease first, checked on a hub, then
  promoted to latest;
- a published tag or asset is never moved or deleted; fixes go in a new
  version.

## License

Proprietary, not open source. You may install and use the hub, unmodified, on
a Home Assistant installation you own or administer, for use with CasaSmart
apps and services. Modifying or redistributing it needs written permission
from CasaSmart. See [LICENSE](LICENSE).
