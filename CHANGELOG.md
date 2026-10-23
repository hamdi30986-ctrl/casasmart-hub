# Changelog

What changed in each version of CasaSmart Hub.

## [Unreleased]
- Turning a room back on now says so when there is nothing to restore,
  instead of reporting success. If a device fails to come back on, the room
  remembers it, so the next tap tries that device again.
- The hub keeps a log of which room devices were switched, when, and how
  (the app, a wall switch, an automation), for the room suggestions coming
  next. It stays on the hub. A member's entries are deleted with their last
  phone, and a factory reset clears it. The database moves to schema v5: to
  go back to 2.3.2, restore the copy the update saves in `casasmart/backups`.
- The Now page now counts a running AC as something on, so a room where only
  the AC runs is no longer hidden. When every room that is on has its own
  power meters, rooms are ordered by how much power they draw; otherwise by
  how many devices are on. Rooms that tie follow your room order. When fewer
  than five rooms are on, rooms that are off fill the empty cards.
- Rooms that are off are now ordered too, each with its reason: first rooms
  where something still runs (a TV or speaker playing, or a device drawing
  real power), then rooms someone switched off in the last 30 minutes
  (newest first; not by an automation or scene, and not the ones you switched
  off yourself), then your room order.

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
