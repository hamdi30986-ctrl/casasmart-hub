# Running the hub on Docker Desktop (Mac)

Only needed when Home Assistant runs in Docker Desktop on a Mac. Home
Assistant OS, Raspberry Pi, Proxmox and Docker on Linux don't need this.

On Docker Desktop, phones can't find the hub and pairing can fail. Two small
helpers on the Mac fix that. They only need `python3`.

## 1. Download the helpers

```sh
git clone https://github.com/hamdi30986-ctrl/casasmart-hub.git ~/casasmart-hub
```

Keep this folder; the helpers run from it.

## 2. Set the hub's port in Docker

In your Docker Compose file:

```yaml
services:
  homeassistant:
    ports:
      - "8123:8123"
      - "127.0.0.1:18443:8443"
```

> Use exactly `127.0.0.1:18443:8443`. Never open port 8443 to the network or
> forward it from your router.

## 3. Start the helpers

```sh
REPO=~/casasmart-hub
PY=/usr/bin/python3
for name in hub-tls-relay hub-mdns; do
  sed -e "s#__PYTHON__#$PY#g" -e "s#__REPO_ROOT__#$REPO#g" \
    "$REPO/deploy/macos/com.casasmart.$name.plist.template" \
    > ~/Library/LaunchAgents/com.casasmart.$name.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.casasmart.$name.plist
done
```

They start automatically when the Mac logs in.

To update them later:

```sh
git -C ~/casasmart-hub pull
launchctl kickstart -k gui/$(id -u)/com.casasmart.hub-tls-relay
launchctl kickstart -k gui/$(id -u)/com.casasmart.hub-mdns
```

## 4. Turn on the Mac setting

1. Stop Home Assistant.
2. Open `casasmart/hub_config.json` in your Home Assistant config folder and
   add `"lan_relay_ingress": "on"`.
3. If you use CasaSmart water tanks, also add
   `"tank_ingest_url": "http://MAC_IP:8123/api/casasmart/tank/reading"`,
   with your Mac's IP address.
4. Start Home Assistant.

## 5. Check it

Open the CasaSmart app on a phone on the same Wi-Fi. The hub should appear
within a few seconds.
