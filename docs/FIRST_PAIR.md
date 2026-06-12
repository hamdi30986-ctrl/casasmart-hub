# 3c-4c — First-Pair & Config-Check Cred Path (Design)

_Status: DESIGN — approved scope from 2026-06-12 chat. This is auth: no code before this doc is agreed._

## Goal

Kill the last hideout of the raw HA token: the bootstrap path. A fresh app
install must acquire the hub **without Supabase ever storing or handing out
HA credentials**. Customer self-pairs on-site, on the hub's LAN, with zero
installer involvement on the phone.

## What exists already (do NOT rebuild)

The hub's Track B machinery covers most of the "hub half":

| Need | Already shipped | Where |
|---|---|---|
| App finds hub on LAN | mDNS `_casasmart._tcp` (B6) | `discovery.py` |
| One-time claim → session cred | `POST /api/casasmart/auth/enroll` — pairing code IS the credential, LAN-only, throttled, single-use | `auth_api.py` / `pairing.py` |
| First-claim = owner | Hub-generated bootstrap **admin** code, redeemable only while no admin enrolled, never expires, dead once owner pairs | `pairing.py` (B3) |
| Member phones later | Admin-minted codes with role+rooms baked in (`sub_admin`/`user` only) | `pairing.py` |
| Reachability check | `GET /api/casasmart/health` + `/handshake` | `api.py` |
| Lost-everything recovery | Recovery code armed at first admin enroll (engraved card) | `recovery.py` |

**3c-4c is therefore mostly an app + Supabase change**, plus a hub gap check.

## Current (legacy) bootstrap — what dies

1. App calls Supabase RPC `claim_pairing_code` → returns `ha_local_url` + raw `ha_token`.
2. App probes HA `/api/websocket` with that token (`ha_reachability_probe.dart`).
3. Token saved on-device; 24h `config_check_service.dart` refreshes it from
   `homes.cloud_ha_url` / `homes.cloud_ha_token`.

Every step above leaks the house key through Supabase. All three go.

## New flow

1. **Prep (Hamdi, at home):** hub flashed with components; hub generates its
   bootstrap admin code → printed as QR sticker on the hub.
2. **Customer:** installs app → app browses mDNS for `_casasmart._tcp` →
   "CasaSmart Hub found" → scans QR / types 6-digit code.
3. **App → hub directly:** `POST /auth/enroll` `{pairing_code, name, public_key}`
   → hub mints the device cred (same hub_auth sessions 3c-1→4 ride).
   Supabase never sees it.
4. **Reachability:** replace the HA websocket probe with hub `/health` +
   `/handshake`. `ha_reachability_probe.dart` is deleted.
5. **Supabase keeps only routing:** home record + tunnel URL for
   away-from-home access. `ha_token` / `cloud_ha_token` columns and the
   `claim_pairing_code` RPC's cred payload are dropped.
6. **Config check slims:** keeps `revoked` + hub/tunnel URL changes; drops
   the token-refresh leg entirely. Revoked flow unchanged.

### Honest trust framing (from the argue session)

Supabase stays in the *routing* path (tunnel URL), not the *credential* path.
A Supabase breach can no longer mint a working session: enroll is LAN-only,
single-use, throttled, and the bootstrap code dies at first claim. What a
breach buys shrinks from "forever HA god-token" to "knowing the tunnel URL".

### Product decisions locked (Hamdi, 2026-06-12)

- Pairing is **always on-site, on the hub's LAN**. Remote pre-provisioned
  pairing is dead by design — no flow needed for it.
- Customer self-pairs; first claim = owner/admin. Installer screens
  (3c-4b) remain Hamdi's on-site surface for device-to-HA pairing.
- Cred recovery / factory reset / handover ride the existing B3 recovery +
  re-claim machinery — re-claim with a new code is the first-class answer.

## Migration for existing installs

Existing homes already run hub sessions (3c-3); their stored `ha_token` is
dead weight. Migration = **wipe, not re-claim**:

- App: on first launch post-update, delete any stored `ha_token` from
  secure storage; no user-visible step.
- Supabase: migration drops the token columns after confirming no app
  version still reads them (config check no longer requests them).

## Work plan

1. **Hub gap check (inline, small):** verify `/auth/enroll` response +
   `/health` expose everything first-pair needs (hub URL confirmation,
   hub id for the Supabase home record). Patch only if a field is missing.
   Live-proof on the dev hub.
   **DONE 2026-06-12 — no patch needed.** Live proof: `.dev/b16_stage3c4c_gapcheck.py`,
   8/8 against the dev hub. Hub id = `handshake.tls.identity_fingerprint_sha256`
   (the same value mDNS advertises as TXT `id` — `__init__.py` passes
   `identity_fingerprint` as `hub_id`), so the app/Supabase home record keys
   off the TLS fingerprint; no new field required. mDNS is only published
   when TLS is up, so a discoverable hub always carries its id. Enroll
   returns `device_id`/`role`/`rooms` and the enrolled key logs in
   immediately; pairing codes proven single-use.
2. **Supabase migration:** slim `claim_pairing_code` → routing-only (or
   retire it), drop token columns. Staged: columns dropped last.
   **RPC SLIM DONE 2026-06-12** — applied to prod
   (`slim_claim_pairing_code_routing_only`), live-proven: payload keys are
   now `id`/`code`/`ha_local_url`/`client_name`, no `ha_token`; bad code →
   `code_not_found` unchanged. Mirror file:
   `CSv1/supabase/migrations/20260612_slim_claim_pairing_code_routing_only.sql`.
   Token columns (`pairing_codes.ha_token`, `homes.cloud_ha_token`,
   `homes.cloud_ha_url`) still present — drop AFTER the app half ships and
   no app version reads them. NOTE: until the app half lands, fresh pairing
   via the current app is intentionally broken (it expects `ha_token`);
   existing installs unaffected (already on hub sessions).
3. **App half (background agent, design locked):** mDNS discovery flow,
   enroll exchange, probe replacement, config-check slim, onboarding/setup/
   settings screens flipped, stored-token wipe, `ha_reachability_probe.dart`
   funeral.
4. **Audit pass** on the diff pair, same as 3c-4a/4b.

## Files in scope

- Hub: `auth_api.py`, `api.py` (only if gap check finds a missing field)
- Supabase: `homes` table, `claim_pairing_code` RPC
- App: `hub_setup_screen.dart`, `onboarding_screen.dart`,
  `hub_settings_screen.dart`, `config_check_service.dart`,
  `ha_reachability_probe.dart` (delete), secure-storage migration shim

Nothing else. No while-we're-here changes.
