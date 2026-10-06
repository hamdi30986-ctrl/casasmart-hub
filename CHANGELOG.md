# Changelog

Release tags are `vX.Y.Z` and always equal `manifest.json`'s `version`; HACS
installs by tag. Earlier tags (v1.8, v2.0, v2.1) omitted the patch digit.

## [2.2.0] - Unreleased

A hardening release. The REST API, WebSocket frames, handshake capabilities and
storage schema (version 4) are unchanged, so existing apps keep working.

### Fixed
- `hacs.json` now requires Home Assistant 2025.3. The code depends on
  `config_entries.async_loaded_entries`, which arrived in 2025.3; on 2025.1–2025.2
  the integration failed at setup.
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
  to GitHub's source zipball, and extracts with strict path containment. It
  remains off unless `update_repo` is configured; update through HACS.

### Changed
- `manifest.json` documentation and issue-tracker links point at this
  repository, and its keys are in hassfest order.
- `hacs.json` hides branch installs, so HACS always records a release tag.
- A startup warning is logged whenever `CASASMART_DEV_ENROLL` enables dev
  auto-enrollment.

### Internal
- Restored the comments and docstrings removed by the 1.7.0 sanitize, wherever
  the code is provably unchanged (AST-checked).
- Restored the original test suite: 1,140 tests, plus about 150 that need a real
  Home Assistant.
- Removed dead code, applied ruff formatting, and added CI (ruff, pytest,
  hassfest, HACS validation, tag/manifest check) and `scripts/release.sh`.
- Moved the app API contracts to `docs/api/`; added a README, this changelog and
  a LICENSE.

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
