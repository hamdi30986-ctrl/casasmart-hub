# Changelog

What changed in each version of CasaSmart Hub.

## [Unreleased]
- One tap on a room that is off now turns on the lights and fans that were
  on last time, however the room was switched off: at the wall, from Home
  Assistant, by an automation or from the app. Never plugs, relays,
  heaters, ACs, covers or TVs. An installer can give a room a start profile
  for when the hub doesn't know yet. With nothing to turn on, the app opens
  the room instead of saying it couldn't turn it on, and the Now page tells
  the apps beforehand. Offline lights are skipped and reported. If a device
  fails to come back on, the next tap tries it again.
- The hub keeps a log of which room devices were switched, when, and how
  (the app, a wall switch, an automation), for the room suggestions coming
  next. It stays on the hub. It starts 10 minutes after Home Assistant
  starts, while devices catch up. A member's entries are deleted with their
  last phone, and a factory reset clears it. The database moves to schema
  v5: to go back to 2.3.2, restore the copy the update saves in
  `casasmart/backups`.
- The Now page now counts a running AC as something on, so a room where only
  the AC runs is no longer hidden. When every room that is on has its own
  power meters, rooms are ordered by how much power they draw; otherwise by
  how many devices are on. Rooms that tie follow your room order. When fewer
  than five rooms are on, rooms that are off fill the empty cards.
- Rooms that are off are now ordered too, each with its reason: first rooms
  where something still runs (a TV or speaker playing, or a device drawing
  real power), then rooms someone switched off in the last 30 minutes
  (newest first; not by an automation or scene), then your room order.
- The hub now learns when each room is usually switched on, from its own
  activity log, and puts those rooms after the ones just switched off, with
  "Usually on around now". Wall switches count as much as an app tap, but
  only for the whole household; automations not at all. It waits for two
  weeks of history first. Each phone blends in its own person's app taps;
  the wall tablet can ask for the whole household's habits instead (once
  the tablet app does). Rooms you switched off yourself in the last 30
  minutes come right after those, marked as yours (not on the household's
  view).
- Ramadan has habits of its own. The hub knows when Ramadan is from the
  Umm al-Qura calendar, offline, and learns those days apart: during
  Ramadan the rooms are ranked on Ramadan habits, and the day after it the
  rest of the year's habits are back exactly as they were before it. Each
  Ramadan is kept for the next one, so next year's starts from day one.
- Admins can turn room habit learning off, pause it, reset it, set the
  weekend days (Friday and Saturday by default), and mark devices that run on
  their own schedule so their timers don't look like habits.
- When a member's last phone leaves the hub, what was learned from them,
  their recently used devices and the room-command answers kept for their
  retries are deleted too.
- A factory reset is for a new owner, so it now leaves nothing of the old
  one to read back: everything the hub learned about rooms goes (the
  activity log, habits, Ramadan habits, last-on lights, start profiles),
  the database is compacted so deleted data doesn't linger on the disk,
  and the database copies earlier updates saved in `casasmart/backups` are
  deleted (after a reset there is no going back to an older version with
  them).

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
