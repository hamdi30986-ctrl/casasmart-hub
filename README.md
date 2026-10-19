# CasaSmart Hub

CasaSmart Hub connects your home to the CasaSmart phone and tablet apps. It
runs inside Home Assistant and gives you:

- secure pairing for each person in the home;
- rooms, favorites and scenes;
- a home security alarm;
- water-tank monitoring;
- speaker announcements and the athan at prayer times;
- Energy Saving;
- push notifications;
- remote access when you're away from home (optional).

## What you need

- Home Assistant 2025.3 or newer, with [HACS](https://hacs.xyz).
- A **relay address** and an **activation code** from your CasaSmart
  installer.
- The CasaSmart app on each phone or tablet.
- Phones on the same Wi-Fi as the hub for the first pairing.

Home Assistant OS needs nothing else. If Home Assistant runs in Docker on
Linux, use host networking. If it runs in Docker Desktop on a Mac, follow the
[Mac guide](deploy/macos/README.md).

## Install

1. In HACS, open the menu → **Custom repositories**, add
   `https://github.com/hamdi30986-ctrl/casasmart-hub` as an **Integration**.
2. Download **CasaSmart Hub**, then restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → CasaSmart Hub**
   and enter the relay address and activation code.
4. Home Assistant shows two codes:
   - the **pairing code**, used once to claim the hub;
   - the **recovery code**, which gets the hub back if the owner loses their
     phone. Write it down and keep it somewhere safe.
5. On a phone at home, open the CasaSmart app, pick the hub and enter the
   pairing code. That phone becomes the owner.
6. Invite everyone else from the app: **Family → Invite member**.

## Remote access

To use the app away from home, set up a Cloudflare tunnel and enter its
address in the integration's **Configure** screen. On Home Assistant OS, the
hub starts and watches the Cloudflare Tunnel add-on for you.

The first pairing and owner recovery always need to happen at home.

## Updating

Update through HACS. When an update appears, tap **Update** and restart Home
Assistant when asked.

## Starting over or handing the hub to someone else

Use **Factory reset** (on the hub's device page in Home Assistant). It unpairs
every phone and issues new pairing and recovery codes. Your devices and
automations in Home Assistant are not touched.

## Common problems

- **"Pairing is only available on the hub's own network"**: connect the phone
  to the home Wi-Fi (not mobile data or a VPN) and try again.
- **"Too many failed attempts"**: wait a minute, then enter the code again.
- **The app can't find the hub**: make sure the phone and hub are on the same
  network and your Wi-Fi doesn't block devices from seeing each other.
- **No notifications**: check Home Assistant's notifications for a CasaSmart
  message. Your installer may need to give you a new activation code.

Still stuck? Contact your CasaSmart installer.

## License

Free to install and use, unmodified, with CasaSmart apps and services. Not
open source. See [LICENSE](LICENSE).
