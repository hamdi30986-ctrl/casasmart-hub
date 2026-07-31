# Energy Saving P1 — engine and persistence

Date: 2026-07-31

Canonical specification: `/Users/macmini/ENERGY_SAVING_PLAN.md` (FINAL SPEC v4)

## Boundary

P1 implements the durable, Home-Assistant-free Energy Saving core:

- strict per-level configuration schema and picker validation;
- resumable per-step config patches;
- setup-complete activation gate;
- persistent active level and lockout state;
- per-device release set with room/source metadata;
- persistent per-room Smart occupancy, including unavailable sensors;
- migration v4 and typed append-only event storage;
- factual stats-lite;
- reboot-safe warm-up and fail-safe coercion of malformed stored data.

P1 does not import Home Assistant and does not execute device commands, timers,
sun logic, temperature logic, role checks, REST views, or WebSocket events.
Those remain assigned to P2 and P3.

## Configuration contract

Each of `low`, `medium`, and `smart` is stored independently in the
`energy_configs` KV namespace:

- `schema_version`;
- `excluded_rooms`;
- `gang_keepers`;
- `light_keepers`;
- `plug_offs`;
- `heaters`: explicit `{entity_id, turn_off}` records;
- `ac_keepers`;
- `setup_complete`;
- `lockout_enabled` on Smart only, default `true`.

Validation enforced in P1:

- Low gang groups keep exactly two entities.
- Medium and Smart gang groups keep exactly one entity.
- Medium configured multi-AC rooms keep exactly one AC.
- Low accepts no AC keeper selection.
- Smart accepts an empty AC keeper list for sensorless rooms whose ACs all turn
  off.
- IDs are non-empty and bounded; device picks are HA entity IDs.
- Duplicate rooms/entities and unknown fields are rejected.
- Heater records are unique and explicitly choose off or keep.
- Excluded rooms cannot also carry light or AC picks.
- Smart flags and setup status are strict booleans.

Candidate-inventory checks that require live discovery—such as validating the
exact `ceil(n/2)` light count—remain a P3 API/discovery responsibility. P1
documents that boundary rather than inventing inventory inside the pure engine.

## State contract

The `energy_state/current` document contains:

- active level and activation/re-apply timestamps;
- effective lockout state;
- sorted released entity IDs plus release metadata;
- Smart room occupancy (`occupied`, sensor availability, change timestamp);
- monotonically increasing revision.

Low and Medium always restore with inherent lockout enabled. Occupancy is
accepted and restored only for Smart. Deactivation clears Energy state and
lockout without restoring any device state. Re-apply clears releases while
preserving current occupancy. Smart room-empty handling can clear only the
departed room's releases through the P2 seam.

## Events and stats

Migration v4 creates `energy_events` with:

- ordered integer ID and UTC epoch timestamp;
- kind, level, optional entity and room;
- strict JSON-object data;
- timestamp and kind/timestamp indexes.

The engine records configuration, activation/deactivation, re-apply, release,
room release-clear, and occupancy events. P2/P3 have a typed audit seam for
rule/automation/command-gate events.

Stats-lite reports only:

- event totals and per-kind counts;
- first/last event time;
- current active level;
- release count;
- occupied, empty, and sensor-issue room counts.

It intentionally contains no cost, money, estimated kWh, or CO2 values.
History is pruned to 180 days during warm-up.

## Verification

Automated:

- focused P1/storage/migration regression: 93 passed;
- full hub suite: 1,010 passed, 145 expected skips;
- Python compile and `tabnanny`: passed;
- `git diff --check`: passed.

Real-data migration rehearsal:

- source: disposable copy of the verified pre-Eco live runtime backup;
- schema: v3 to v4;
- SQLite integrity: `ok` before and after;
- existing KV rows: 360 before and 360 after;
- migration framework created one automatic v3 backup;
- new `energy_events` table started empty with the expected seven columns.

No P1 code was deployed to the running Home Assistant container.

## Backups and repository isolation

The actual Docker-mounted runtime was backed up with SQLite's online-backup API
before any deployment:

`/Users/macmini/hub-backups/casasmart-live-pre-eco-20260731-115749`

- runtime DB SHA-256:
  `c4cac741a9442a5cbcef74ac6c8968cbe3409adb3298309b4add9e0662dd8abc`;
- runtime archive SHA-256:
  `ad16db36c586a324ef5d9dbc3c426edb2210ff1a1b817e99f072606b63d4849e`;
- source and backup schema v3, integrity `ok`, 360 KV rows;
- archive and database are owner-only inside an owner-only directory.

The earlier git/source backup remains at
`/Users/macmini/hub-backups/casasmart-hub-pre-eco-20260731-103856`.
`CSv1` and `CSv1-tablet` are untouched by P1.
