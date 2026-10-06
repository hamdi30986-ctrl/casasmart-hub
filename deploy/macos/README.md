# Running the hub on Docker Desktop (macOS)

Home Assistant OS, a Raspberry Pi, a Proxmox VM or Docker on Linux need none of
this. Use this guide only when Home Assistant runs in Docker Desktop on a Mac.

Docker Desktop causes two problems for the CasaSmart app:

- **Phones can't discover the hub.** A container can't announce itself on the
  LAN over multicast DNS (`_casasmart._tcp`).
- **The hub can't tell which phones are on the LAN.** Docker Desktop rewrites
  the source address of every connection that reaches the container, and after
  a container restart it can even show a public address. Pairing, owner
  recovery and keyless speaker provisioning are LAN-only, so without help they
  are refused.

Two small helpers run on the Mac and fix both. They need only `python3` and
its standard library.

- `tls_relay.py` listens on the Mac's port 8443, admits only clients from
  local address ranges (private, link-local, loopback), and forwards the
  encrypted bytes to the hub's TLS port. It doesn't terminate TLS, see pairing
  codes, or hold any hub identity.
- `mdns_publish.py` reads the hub's public handshake and advertises
  `_casasmart._tcp` through macOS Bonjour with the hub's real fingerprint. It
  does this on the Mac's LAN address, port 8443.

## 1. Get the helpers

HACS installs only the integration, not this folder. Clone the repository on
the Mac and keep that checkout in place, because launchd runs the helpers from
it:

```sh
git clone https://github.com/hamdi30986-ctrl/casasmart-hub.git ~/casasmart-hub
```

## 2. Publish the hub's TLS port to loopback only

In your Docker Compose file, map the container's port 8443 to `127.0.0.1:18443`
only, so the relay is the only way in from the network:

```yaml
services:
  homeassistant:
    ports:
      - "8123:8123"
      - "127.0.0.1:18443:8443"
```

> **Never publish 8443 on all interfaces, and never forward it from your router.**
> Step 4 tells the hub to treat every connection on its TLS port as coming from
> the LAN. The loopback-only mapping plus the LAN-only relay is what makes that
> safe. For the same reason, don't point anything on the Mac that carries outside
> traffic at `127.0.0.1:18443` or `8443`: Tailscale Serve or Funnel, ngrok,
> `ssh -R`, or a reverse-proxy container.

## 3. Install the launch agents

Replace the two placeholders and load the agents:

```sh
REPO=~/casasmart-hub          # absolute path of the checkout from step 1
PY=/usr/bin/python3           # any python3 (Command Line Tools or Homebrew)
for name in hub-tls-relay hub-mdns; do
  sed -e "s#__PYTHON__#$PY#g" -e "s#__REPO_ROOT__#$REPO#g" \
    "$REPO/deploy/macos/com.casasmart.$name.plist.template" \
    > ~/Library/LaunchAgents/com.casasmart.$name.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.casasmart.$name.plist
done
```

Both agents start at login and restart if they exit. They log to
`/tmp/casasmart-hub-tls-relay.log` and `/tmp/casasmart-hub-mdns.log`.

- The relay's default ports (8443 in, `127.0.0.1:18443` out) match step 2.
- The publisher finds the Mac's LAN address itself. It reads the handshake
  from `https://127.0.0.1:18443` and advertises the name "CasaSmart Hub". To
  change any of these, add `--address`, `--handshake` or `--name` to its plist.

To update the helpers later, pull the checkout and restart them:

```sh
git -C ~/casasmart-hub pull
launchctl kickstart -k gui/$(id -u)/com.casasmart.hub-tls-relay
launchctl kickstart -k gui/$(id -u)/com.casasmart.hub-mdns
```

To remove them, run `launchctl bootout gui/$(id -u)/com.casasmart.hub-tls-relay`
(and the same for `hub-mdns`), then delete the two plists from
`~/Library/LaunchAgents`.

## 4. Tell the hub to trust its TLS port

On Docker Desktop the hub can't use a client's address to decide "is this phone
on the LAN?", so pairing, owner recovery and keyless speaker provisioning are
refused until you tell it to trust its TLS port instead. Do this only after
steps 2 and 3, because the hub can't check them itself:

1. Stop Home Assistant.
2. Open `casasmart/hub_config.json` inside the Mac folder you mount as
   `/config`, and add `"lan_relay_ingress": "on"`.
3. If you use CasaSmart water tanks, also add
   `"tank_ingest_url": "http://MAC_LAN_IP:8123/api/casasmart/tank/reading"`.
   Inside Docker Desktop the hub only knows its container address, which tank
   sensors on the LAN can't reach.
4. Start Home Assistant.

The hub then logs a WARNING that starts with "LAN relay ingress on". Until you
set it, the hub logs a WARNING that starts with "Docker Desktop detected" at
every start. See [Hub settings](../../README.md#hub-settings).

Requests that crossed Cloudflare are never LAN, whatever this says. Home
Assistant's own port (8123, where a tunnel enters) is never trusted this way.

Behind the relay every phone looks the same to the hub, so they share one
pairing throttle. After five wrong codes from anyone, everyone waits a minute.

## 5. Check it

From another device on the LAN (`MAC_LAN_IP` is the Mac's address on the LAN):

```sh
curl -k https://MAC_LAN_IP:8443/api/casasmart/handshake
dns-sd -B _casasmart._tcp local.
```

The first command returns the hub's handshake JSON. The second lists the hub
within a few seconds (macOS; on Linux use `avahi-browse -r _casasmart._tcp`).
