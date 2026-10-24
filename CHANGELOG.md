# Changelog

What changed in each version of CasaSmart Hub.

## [2.3.3] - 2026-10-24
- Smarter room order on the Now page: rooms in use first, then rooms just
  switched off, then the rooms you usually use at this time.
- A room with its AC running now shows as on.
- Turning a room off switches off its lights, fans and wall switches, and
  leaves plugs alone.
- One tap turns a room back on with the lights and fans that were on last
  time.
- The hub learns your usual rooms over time, with Ramadan kept apart.

## [2.3.2] - 2026-10-22
- Admins can now choose which cameras each member sees: none, only the
  cameras in their rooms, or all of them. Existing members keep what they
  see today.
- Cameras can be placed in a room, like any other device.
- Deleting a room also removes it from every member who had it.

## [2.3.1] - 2026-10-21
- The phone's Doors pill now counts all door and window sensors until you pick a list.

## [2.3.0] - 2026-10-08

A reliability and security update. Your phones stay paired and the apps keep
working as before.

### Before you update

- **Hubs on Docker Desktop (Mac):** add `"lan_relay_ingress": "on"` to
  `hub_config.json`, as the [Mac guide](deploy/macos/README.md) explains.
- **Speakers:** if your speakers stop getting their settings after the
  update, ask your installer.

### What's new

- Stronger security and better protection of your home's information.
- More reliable pairing, remote access and notifications.
- Faster restarts, and fewer errors on slow or unusual setups.
- New CasaSmart icon in Home Assistant.
- Device names in Home Assistant are shorter and clearer.
- Only Home Assistant admins can factory reset the hub.
- Many smaller fixes.

## [2.1.0] - 2026-10-05
- Room suggestions on the tablet's NOW page, such as turning off a busy room
  or saving energy in it. Nothing runs until you confirm.
- Fixes for moving devices between rooms.

## [2.0.0] - 2026-10-04
- New bright room-tag colors for the tablet app.

## [1.9.0] - 2026-10-04
- Room tags.

## [1.8] - 2026-08-31
- CasaSmart reminder notifications.
- Support for hubs running in Docker Desktop on a Mac.

## [1.7.0] - 2026-08-19
- First public release.
