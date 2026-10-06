# Changelog

Release tags are `vX.Y.Z` and always equal `manifest.json`'s `version`; HACS
installs by tag. Earlier tags (v1.8, v2.0, v2.1) omitted the patch digit.

## [2.3.0] - Unreleased

A hardening release for hubs installed by anyone, not just one house. The REST
API, WebSocket frames, handshake capabilities and storage schema (version 4)
are unchanged, so existing apps keep working. A 2.2.0 was prepared but never
published; everything it contained is in 2.3.0.

### Upgrade notes
- **Docker Desktop hubs:** add `"lan_relay_ingress": "on"` to
  `/config/casasmart/hub_config.json` (with Home Assistant stopped), as step 4
  of [deploy/macos](deploy/macos/README.md) describes. Until then, pairing,
  owner recovery and keyless speaker provisioning are refused, and the hub
  logs a warning at every start. Other hubs need nothing.
- Entity friendly names now read "CasaSmart Hub Factory reset", "CasaSmart Hub
  Energy savings" and so on, instead of repeating "CasaSmart". Entity IDs are
  unchanged.
- HQ reminder pushes are titled "CasaSmart HQ" unless a sender name is set
  (`casasmart.configure_hq_notifications`, new `sender_name` field).

### Removed
- The `pairing_extra_lan_cidrs` hub setting. Private and link-local addresses
  already counted as LAN and public ranges were refused, so its only effect was
  to let loopback count as LAN, which is how a local tunnel arrives. It is now
  ignored, and setup logs a warning while it is still in `hub_config.json`.

### Added
- Brand icons (`brand/icon.png`, `brand/icon@2x.png`), which Home Assistant
  2026 shows for the integration.
- `lan_relay_ingress` hub setting: `"on"` trusts the hub's TLS port as the LAN
  proof behind the Docker Desktop relay; `"off"` (the default) checks client
  addresses. Any other value logs a warning and counts as `"off"`.
- `sender_name` for `casasmart.configure_hq_notifications`: one line of up to
  40 characters, without control characters or bidi overrides.

### Fixed
- Pairing a new phone, owner recovery and keyless speaker provisioning can work
  on Docker Desktop again. Docker Desktop rewrites every source address
  reaching the container (after a restart it can even pick a public one), so
  these LAN-only requests were refused. With `lan_relay_ingress` on, the hub
  trusts its loopback-published TLS listener behind the LAN-only relay. The
  hub can't verify that setup itself, so it never does this on its own.
- Several phones pairing (or using the app) at the same moment no longer fail
  at random. Storage reads were finished outside the database lock, so a
  concurrent write could make a just-paired phone look unknown (login 404) or
  return a torn row (500). About 1 in 8 phones failed when 8–12 paired at once.
- The daily tank low-water check runs at 18:00 in Home Assistant's time zone,
  and its once-a-day limit follows the local calendar day. Both were fixed to
  UTC+3, so hubs elsewhere checked at the wrong hour.
- Alarm alerts no longer log "push not yet wired"; the WARNING now reads
  "Alarm alert (<kind>)". Push to phones was never affected.
- The recovery-code notification no longer claims the code survives a factory
  reset (a reset replaces it), and the factory-reset notification no longer
  claims rooms and scenes are kept.
- Alarm and lock alerts are delivered to every registered device when device
  roles can't be resolved. Since 1.8 they were dropped in that case. Tank,
  device-paired and HQ reminder pushes still fail closed.
- Requests carrying Cloudflare's proxy headers are never treated as LAN, which
  keeps pairing, owner recovery and speaker provisioning LAN-only even for a
  tunnel pointed at the hub's own TLS listener. Remote pairing through the
  tunnel still works when `remote_pairing_enabled` is set.
- Pairing-code hash lookups compare in constant time everywhere.
- The tank ingest throttle keys on Home Assistant's resolved client address, so
  client-sent `CF-Connecting-IP` / `X-Forwarded-For` headers can no longer dodge it.
- Factory reset also clears room tags and room-move receipts. `services.yaml`
  and the reset button now describe what is cleared and kept.
- The built-in updater only installs the signed `casasmart.zip` release asset.
  It finds the integration at the zip root (the HACS layout), never falls back
  to GitHub's source zipball, extracts with strict path containment, and does
  the extraction and swap off Home Assistant's event loop. It remains off
  unless `update_repo` is configured; update through HACS.

### Changed
- The home's coordinates are no longer logged at INFO when prayer times are
  scheduled (DEBUG only), since logs get pasted into public issues.
- The HQ reminder service and pushes no longer carry a personal name.
- `LICENSE` now grants free use of the unmodified hub with CasaSmart; it
  remains proprietary.
- `manifest.json` names the repository owner as codeowner, its documentation
  and issue-tracker links point at this repository, and its keys are in
  hassfest order. hassfest now passes: `network` is declared as a dependency
  (zeroconf already loaded it), the prayer-times requirement is `>=1.0.3`
  (Home Assistant ships 1.0.3), and the relay help texts use an
  `{example_url}` placeholder instead of a literal URL.
- `hacs.json` declares the minimum Home Assistant version, 2025.3 (tested on
  2025.3, 2026.4 and 2026.9), and hides branch installs, so HACS always
  records a release tag.
- A startup warning is logged whenever `CASASMART_DEV_ENROLL` enables dev
  auto-enrollment.

### Internal
- A README for people installing the hub, a Docker Desktop guide they can
  follow, this changelog, and API contracts in `docs/api/`.
- Restored the comments and docstrings removed by the 1.7.0 sanitize, wherever
  the code is provably unchanged (AST-checked). Comments no longer carry
  internal plan references, and the ones that described old behaviour were
  corrected.
- Restored the original test suite: 1,344 tests, about 155 of which need a real
  Home Assistant. Test data no longer contains anyone's network, names or
  devices.
- Removed dead code, applied ruff formatting, and added CI (ruff, pytest with
  and without Home Assistant, hassfest, HACS validation, tag/manifest check)
  and `scripts/release.sh`, which builds and signs before it tags.

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
