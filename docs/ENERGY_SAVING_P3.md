# Energy Saving P3 — API, lockout, automation gate, and runtime wiring

Date: 2026-07-31

Canonical specification: `/Users/macmini/ENERGY_SAVING_PLAN.md` (FINAL SPEC v4)

## Boundary

P3 connects the P1 engine and P2 adapter to CasaSmart's established Home
Assistant surfaces:

- authenticated REST state/config/discovery/control views;
- live-inventory validation before a wizard can be completed;
- hub-side device and scene lockout enforcement;
- scene and automation `works_during_energy_saving` flags;
- exact disable/restore bookkeeping for HA automations;
- content-free `energy_changed` WebSocket nudges;
- `sensor.casasmart_energy_savings`;
- startup/reboot coordination, unload cleanup, and factory-reset wiping.

P3 contains no Flutter/mobile/tablet changes. Nothing was deployed to or tested
against the live Home Assistant instance; live E2E remains P8.

## REST contract

All routes are under `/api/casasmart/energy` and use CasaSmart JWT auth:

- `GET /state` — `energy.read`; active level, lockout, scoped room occupancy,
  and admin-only releases/issues/stats;
- `GET /discovery` — `energy.manage`; floor-grouped wizard inventory;
- `GET/PATCH/DELETE /config/{level}` — `energy.manage`; read, resumable update,
  or reset one level;
- `POST /activate` — `energy.control`; body contains `level` and optional Smart
  `lockout_enabled`;
- `POST /deactivate` — `energy.control`;
- `POST /reapply` — `energy.control`.

The role matrix is centralized in `auth_engine.py`:

- `energy.read`: admin, sub-admin, user;
- `energy.control`: admin only;
- `energy.manage`: admin only.

Activation of an unfinished level returns HTTP 409 with
`{"error":"setup_required","level":"..."}`. Re-apply while inactive and a
second activation also use machine-readable 409 responses.

## Discovery and validation

Discovery returns deterministic floor and room ordering plus:

- climates;
- real lights, excluding gang-backed lights, with dimmable capability;
- two/three-channel gang layouts in physical channel order;
- plug candidates with same-device power-sensor siblings when available;
- covers and heater/outlet candidates;
- temperature/presence sensor inventory and the Smart automatic-room flag.

Nested gang metadata is preferred; the legacy flat `gang_types`/`gang_names`
shape remains readable during migration.

Every PATCH is validated against the current inventory before persistence.
Partial wizard steps may be saved while `setup_complete=false`. Completion
requires every eligible gang/light/AC/heater decision, exact gang counts,
`ceil(n/2)` light keepers, and no stale, foreign, or excluded-room picks.

## Lockout and scene behavior

The existing device command path enforces lockout after the unknown/scope 404
gate and before parsing or executing the command:

- non-admin + effective lockout -> HTTP 403 `energy_lockout`;
- admin commands remain available and their resulting state edges enter P2's
  per-device release ledger;
- alarm endpoints are separate and remain exempt.

Scene activation follows the same privacy order. A locked family role receives
403 `energy_lockout`; an admin, or any role while Smart lockout is disabled,
receives 409 `scene_skipped_energy_saving` for an unflagged scene. Flagged
scenes run through the existing shared executor, so their device changes become
normal P2 releases. The internal `casasmart.activate_scene` HA service also
refuses unflagged scenes while a level is active.

Scene flags live on registry scene records and default false for legacy/new
records. Automation flags live only in the dedicated `energy_flags` KV
namespace. Although the existing scene/automation editors are also available
to sub-admins, changing the Energy Saving flag itself requires `energy.manage`
(admin).

## Automation lifecycle safety

On activation/recovered startup, every enabled unflagged `automation.*` entity
is turned off. Each successful disable is persisted immediately in one exact
set. Deactivation restores only that set and removes entries only after a
successful `turn_on`; failures remain durable and retry on startup/deactivation.
Disable, restore, and failure outcomes are recorded in `energy_events`.

Factory reset deactivates first and refuses to wipe the restore ledger if any
automation could not be restored. It then clears `energy_configs`,
`energy_state`, `energy_flags`, and `energy_events` with the other owner data.
No device state is restored.

## Realtime and HA entity

Every API state/config transition and every P2 release/occupancy transition
fires `EVENT_ENERGY_CHANGED`. Authorized sockets receive only
`{"type":"energy_changed"}`; the frame is coalescible and contains no room,
device, or configuration data. Apps re-fetch REST through their own role and
room scope.

`sensor.casasmart_energy_savings` reports `off`, `low`, `medium`, or `smart` and
exposes lockout, release count, room occupancy, issues, and revision as HA
attributes. It is event-driven and does not poll.

## Verification

- Python compilation: passed;
- focused P1/P2/P3 engine, adapter, runtime, validation, registry, and WS tests:
  passed;
- full local hub suite: 1,056 tests total — 906 passed and 150 expected
  environment-only skips;
- `git diff --check`: passed.

The view-contract suite additionally pins permission responses, 409
`setup_required`, 403 command lockout, and scene 403-vs-409 behavior when run
inside the real-Home-Assistant CI/hub environment.
