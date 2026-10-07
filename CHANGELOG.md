# Changelog

Release tags are `vX.Y.Z` and always equal `manifest.json`'s `version`; HACS
installs by tag. Earlier tags (v1.8, v2.0, v2.1) omitted the patch digit.

## [2.3.0] - Unreleased

A hardening release for hubs installed by anyone, not just one house. The REST
API, WebSocket frames, handshake capabilities and storage schema (version 4)
change only by the stricter input checks below, so the current phone and
tablet apps keep working. 2.2.0 was never published; everything in it is here.

### Upgrade notes

- **Docker Desktop hubs:** add `"lan_relay_ingress": "on"` to
  `/config/casasmart/hub_config.json` (with Home Assistant stopped), as step 4
  of [deploy/macos](deploy/macos/README.md) describes. Until then, pairing and
  owner recovery work or fail depending on the address Docker Desktop shows
  after each restart, and the hub logs a warning at every start. Other hubs
  need nothing.
- **Speakers:** a speaker now needs the hub's provisioning key to fetch its
  broker settings. If your speakers don't send the key, set
  `"keyless_speaker_provisioning": true` in `hub_config.json` before
  upgrading, or they are refused the next time they fetch their settings. The
  hub logs a warning while it is on.
- **Entity names** now read "CasaSmart Hub Factory reset", "CasaSmart Hub
  Energy savings" and so on, instead of repeating "CasaSmart". Entity IDs are
  unchanged.
- **HQ reminder pushes** are titled "CasaSmart HQ" unless a sender name is set
  (the new `sender_name` field of `casasmart.configure_hq_notifications`).

### Added

- `lan_relay_ingress` hub setting: `"on"` trusts the hub's TLS port as the
  local network behind the Docker Desktop relay; `"off"` (the default) checks
  client addresses. Any other value logs a warning and counts as `"off"`.
- `keyless_speaker_provisioning` hub setting (default `false`): lets speakers
  on the local network fetch their broker settings without the provisioning
  key.
- `sender_name` for `casasmart.configure_hq_notifications`: one line of up to
  40 characters, without control characters or bidi overrides.
- Brand icons, which Home Assistant 2026 shows for the integration.

### Changed

- The home's coordinates are logged at debug level only, not at info when
  prayer times are scheduled, since logs get pasted into public issues.
- The HQ reminder service and pushes no longer carry a personal name.
- `LICENSE` grants free use of the unmodified hub with CasaSmart; it remains
  proprietary.
- `manifest.json` names the repository owner as codeowner, links to this
  repository, declares `network` as a dependency, relaxes the prayer-times
  requirement to `>=1.0.3` (the version Home Assistant ships), and passes
  hassfest.
- `hacs.json` declares the minimum Home Assistant version, 2025.3 (tested on
  2025.3, 2026.4 and 2026.9), and hides branch installs, so HACS always
  records a release tag.
- A startup warning is logged whenever `CASASMART_DEV_ENROLL` turns on dev
  auto-enrollment.

### Removed

- The `pairing_extra_lan_cidrs` hub setting. Its only effect was to let
  loopback count as local, which is how a local tunnel arrives. It is ignored
  now, and setup logs a warning while it is still in `hub_config.json`.

### Fixed

#### Access control

- A home-screen widget's token can only read and control devices. It could
  also change its device's push registration (sending alarm and lock alerts
  elsewhere), unpair the device (leaving the hub unclaimed when that was the
  owner's), and write per-person settings, favorites and suggestions.
- Speaker controls respect a member's rooms: speakers outside them can't be
  controlled, and an announcement to all speakers reaches only the member's
  own.
- A sub-admin can't be limited to rooms by any path, the developer manifest
  included; only the user role is room-scoped.
- A WebSocket whose token fails its periodic re-check stops receiving home
  data at once, not up to 30 seconds later.
- Requests carrying Cloudflare's proxy headers never count as local, so
  pairing, owner recovery and keyless speaker provisioning stay local even
  through a tunnel pointed at the hub's own TLS port. Remote pairing still
  works when `remote_pairing_enabled` is set.
- The tank ingest throttle keys on Home Assistant's resolved client address,
  so client-sent `CF-Connecting-IP` or `X-Forwarded-For` headers can't dodge
  it.
- Pairing-code hashes are compared in constant time everywhere.

#### Pairing and owner recovery

- Pairing and owner recovery work on Docker Desktop again with
  `lan_relay_ingress` on. Docker Desktop rewrites the source address of every
  connection, sometimes to a public one, so these local-only requests were
  refused.
- Several phones pairing or using the app at once no longer fail at random
  with a 404 or 500: a storage read could see another phone's half-finished
  write. About 1 in 8 failed when 8 to 12 phones paired together.
- The recovered owner keeps their favorites and settings, and the lost
  phone's push token is removed, so it stops receiving alarms. Trying the
  recovery card on a hub that has no owner yet no longer invalidates the
  card.
- A pairing request with a bad name or key no longer uses up the code.
- A pairing or recovery code with non-ASCII characters counts as a wrong code
  instead of causing a server error.

#### Alarm and push notifications

- Entry delay: sensors the armed mode watches follow a running entry delay
  instead of triggering at once, so walking past a hallway motion sensor on
  the way to the app no longer sets the alarm off. Sensors the mode ignores
  (motion in Night) stay ignored, and life-safety sensors still trigger at
  once.
- A door or window left open while armed no longer sets the alarm off when its
  sensor reports an unrelated attribute change, and an entry delay whose timer
  fires a moment early no longer leaves the alarm pending for good.
- Alarm and lock alerts go to every registered device when device roles can't
  be read; since 1.8 they were dropped. Tank, device-paired and HQ reminder
  pushes are still withheld then.
- Alarm alerts no longer log a misleading "push not yet wired" warning; push
  to phones always worked.
- HQ reminders: a retry arriving on the other port sends one push, not two; a
  request whose body arrives in more than one piece is no longer rejected; and
  stored delivery records are capped at 1,000.

#### Energy Saving

- Energy Saving never controls a room the owner excluded. Motion or
  temperature changes in an excluded room (the kitchen and bathroom by
  default) still switched its AC, fans and lights.
- Fahrenheit homes work: readings are converted to Celsius and setpoints back
  to the home's unit. Before, a Fahrenheit home boosted every occupied room's
  AC.
- Start, stop and re-apply run one at a time, and a stop cancels a start in
  progress. Stopping while it was still starting could leave automations off
  with nothing to restore them.
- A rule part-way through when Energy Saving stops sends no further commands,
  and a failed save leaves the live state as it was.

#### Rooms, scenes and devices

- Color temperature works on Home Assistant 2026, which accepts and reports
  kelvin only: the apps' mired values in commands and scene steps are
  converted, and kelvin-only lights also get `color_temp`, `min_mireds` and
  `max_mireds`. Before, every color-temperature command and scene step was
  refused on 2026.
- A second room-off (from another member, or a retry) no longer erases what
  room-on restores, and a room's commands run one at a time whichever route
  they arrive on.
- A scene keeps going when Home Assistant rejects one of its steps, and a
  device command that worked is no longer reported as failed when a storage
  read after it fails.
- The WebSocket never sends an entity's removal after its newer state, which
  made the apps drop the tile of a device that still exists.
- A tag whose last room moves to another tag is deleted, as when its last room
  is deleted; before, it lingered unseen and kept its name taken. A room can
  no longer be created on a floor that is being deleted.
- A partial device edit can no longer assign one switch to two devices.
- Automation edits arriving on the LAN port and through the tunnel at the same
  moment no longer overwrite each other.
- Generated suggestions work with an AC that reports no fan modes, and a
  failed read or write of `automations.yaml` answers with a JSON error.

#### Speakers and athan

- While the MQTT broker is unreachable, speaker commands, announcements and PA
  answer 503 instead of being queued and played late, and an athan that can't
  be delivered is skipped. Two broker changes at once no longer leave two MQTT
  clients knocking each other off the broker.
- A prayer is never played twice (the hourly re-arm could replay one that had
  just played), and a malformed stored time zone no longer stops the hub from
  loading.

#### Water tanks

- The daily low-water check runs at 18:00 in Home Assistant's time zone, and
  its once-a-day limit follows the local calendar day. Both were fixed to
  UTC+3, so hubs elsewhere checked at the wrong hour.
- A tank whose setup failed while uploading its script can be set up again;
  before, every retry was refused as "already registered".
- A tank whose Shelly sent an unexpected reply during setup can be set up
  again, and `0.0.0.0` is refused as a Shelly address.

#### Self-update

- The built-in updater installs only the signed `casasmart.zip` release
  asset. It finds the integration at the zip root (the HACS layout), never
  falls back to GitHub's source zipball, and extracts with strict path
  containment. It stays off unless `update_repo` is set; update through HACS.
- The rollback copy and staging folders live in `<config>/casasmart/update/`,
  not in `custom_components`, where Home Assistant could load the old copy in
  place of the integration. Leftovers from earlier versions are moved there at
  startup.
- Only one self-update runs at a time (a second gets "update already in
  progress"), and the rollback copy survives a failed swap.
- The update no longer blocks Home Assistant's event loop with file work, and
  a malformed reply from GitHub no longer fails the update check.

#### Setup, settings and stability

- A hub setting whose save fails keeps its live value, and an unwritable data
  folder gives a clean error instead of a raw permission error, also from the
  `set_tunnel_url`, `configure_hq_notifications` and `factory_reset` services.
- A `tls_port` that isn't a port number falls back to 8443 with a warning
  instead of stopping setup.
- An interrupted database upgrade step (a power cut, say) is rolled back as a
  whole, so the next start can run it again instead of failing every time.
- If Home Assistant can't unload the hub's entities, the hub reports it
  instead of shutting down anyway.
- A long hub name in a non-Latin script no longer stops the hub from being
  discovered.
- No Home Assistant 2026.9 deprecation warning about
  `device_registry.devices`, which stops working in 2027.9.
- The macOS TLS relay handles an upstream timeout on the system Python 3.9,
  and judges IPv4-mapped IPv6 peers by their IPv4 address.

#### Input checks

- Malformed input gets a 400 or 401 instead of a server error: non-ASCII
  tokens, provisioning keys and HQ timestamps; a non-string `expires_in`,
  speaker command field or Now scene field; malformed PA uploads and
  push-token bodies; a non-string gang presentation, widget tile type or
  config-flow handler; NaN or infinite numbers in scene data; and very large
  tank reading windows.
- `PUT /now/rooms/{id}/activity` answers 405. It used to change the room's
  activity policy, which belongs to `/activity-policy`.

#### Remote access

- Clearing the Cloudflare domain gives cloudflared back its start at boot.
- Tunnel URLs with a bad port or whitespace are no longer accepted or
  advertised.

#### Notifications and descriptions

- The recovery-code notification no longer claims the code survives a factory
  reset (a reset replaces it), and the factory-reset notification no longer
  claims rooms and scenes are kept.
- Factory reset also clears room tags and room-move receipts. `services.yaml`
  and the reset button describe what is cleared and what is kept.

### Internal

- A README for people installing the hub, a Docker Desktop guide, this
  changelog, and API contracts in `docs/api/`.
- The comments and docstrings removed by the 1.7.0 sanitize are back wherever
  the code is provably unchanged (AST-checked), rewritten to be short and
  without internal plan references.
- The original test suite is back: about 1,600 tests, some 230 of which need
  a real Home Assistant. Test data no longer contains anyone's network, names
  or devices.
- Dead code removed, ruff formatting applied, and CI added (ruff, pytest with
  and without Home Assistant, hassfest, HACS validation, tag/manifest check),
  with `scripts/release.sh`, which builds and signs before it tags.

## [2.1.0] - 2026-10-05
- Generated room-scene suggestions for the tablet NOW page (turn off the most
  active room, save energy in it, turn off the second most active room), always
  user-confirmed.
- Corrective changes for atomic room moves, contextual suggestion error handling
  and storage recovery.

## [2.0.0] - 2026-10-04
- Bright room-tag palette (yellow, green, white, red) for CasaSmart Tablet v1.1.

## [1.9.0] - 2026-10-04
- Persistent room tags with authenticated CRUD in the registry API.

## [1.8] - 2026-08-31
- Authenticated HQ reminder delivery.
- macOS Docker LAN TLS relay and dynamic mDNS publisher.

## [1.7.0] - 2026-08-19
- First release of this repository (sanitized HACS distribution).
