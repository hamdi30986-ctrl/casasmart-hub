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
> On Docker Desktop the hub treats every connection on its TLS port as coming
> from the LAN (see step 4). The loopback-only mapping plus the LAN-only relay
> is what makes that safe.

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

## 4. How the hub decides "LAN" here

On Docker Desktop the hub can't use a client's address, so the hub setting
`lan_relay_ingress` decides instead:

- `"auto"` (the default) trusts the hub's TLS listener as the LAN when the hub
  runs under Docker Desktop (its VM kernel reports `linuxkit`). Elsewhere the
  hub checks addresses.
- `"on"` always trusts the TLS listener. Use it for another setup where the TLS
  port is reachable only through a LAN-only relay.
- `"off"` always checks addresses.

Requests that crossed Cloudflare are never LAN, whatever this says. Home
Assistant's own port (8123, where a tunnel enters) is never trusted this way.

At startup the hub logs a WARNING that starts with "LAN relay ingress on" when
it trusts the listener. To change the setting, see "Hub settings" in the
[main README](../../README.md#hub-settings).

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
