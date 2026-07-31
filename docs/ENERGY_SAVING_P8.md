# Energy Saving P8 — live development-hub verification

Date: 2026-07-31

Canonical specification: `/Users/macmini/ENERGY_SAVING_PLAN.md` (FINAL SPEC v4)

## Boundary and safety

P8 was run against the local development Home Assistant instance only:

- development hub: `192.168.8.236:8123` and its TLS relay on `:8443`;
- panel: `192.168.8.170:39305`;
- production hub `192.168.8.61:8123` was explicitly excluded;
- production mobile repository `/Users/macmini/CSv1` remained clean and
  untouched.

Before deployment, an online SQLite backup, the deployed CasaSmart component,
the handshake identity, and container state were saved under
`/Users/macmini/hub-backups/p8-20260731-180705`. The backup database passed
`PRAGMA integrity_check`.

Only the explicit P1-P3 component allowlist was deployed. Repository
`hub.db`, bytecode caches, tests, and documentation were not copied into Home
Assistant. The deployed component compiled successfully before restart.

## Tablet installation and UI verification

The signed release APK from `/Users/macmini/CSv1-tablet` was installed with
`adb install -r`, preserving app data, Device Owner, default Home, and Android
lock-task state. The installed package is `com.casasmart.casasmart_app`, version
`1.0.0` (`versionCode 1011`), signed by the expected panel certificate.

Both Recommended and Custom wizard paths were walked on the physical panel for
Low, Medium, and Smart, including every picker/review step, Smart's sensor
advisory and activation lockout toggle, live plug draw, blink-to-identify, RTL
Arabic layout, and stop-before-reconfigure behavior.

The first release run exposed a dynamic-dispatch-only Flutter failure when an
`AsyncLoading` value reached the Energy listener. The listener is now
statically typed and covered for loading, error, and data states in tablet
commit `f6da5d2`. The corrected release APK was rebuilt, reinstalled, and
retested through the loading-to-data transition without Flutter errors.

## Live hub matrix

Discovery returned 3 floors, 9 rooms, 6 climates, 32 lights, and two automatic
rooms with temperature/presence pairs. The development registry currently has
no Energy-eligible gangs, plugs, heaters, or covers.

The following cases passed against real Home Assistant entities:

- Low: valid config/activation, 24 degree cooling floor, unchanged fan rule,
  unpicked-light shutdown after device acknowledgement, and inherent member
  lockout.
- Medium: with two ACs initially prepared on, the keeper remained on at 24
  degrees/fan low and the non-keeper turned off; member control returned
  `403 energy_lockout`.
- Smart lockout: enabled returned `403 energy_lockout`; disabled allowed the
  sub-admin command and recorded the affected light as released.
- Smart empty grace: a manual light override remained active at +41 seconds;
  after the 45-second boundary it turned off, the room became empty, and its
  release self-expired.
- Re-apply/release surfaces and admin state responses remained coherent.
- Scene gate: locked member `403`, unflagged admin scene
  `409 scene_skipped_energy_saving`, flagged admin scene executed and its light
  became released.
- Automation gate: activation disabled all 16 previously enabled unflagged
  automations; a temporary flagged automation stayed enabled, executed, and
  its light became released.
- Reboot recovery: Smart and lockout survived Home Assistant restart; all 20
  automations remained disabled while active; deactivation restored the exact
  pre-test 16-on/4-off set.

Every deliberately changed AC/light was restored to its captured prior state.
The temporary scene and automation were deleted.

## Hardware-limited cases

The live development inventory cannot safely drive every dynamic branch:

- its only automatic room with an AC reports a room temperature of `0.0`, and
  the other automatic room has no AC;
- the real presence sensors are read-only, so an artificial occupied edge was
  not injected into the household sensor topics;
- no Energy-eligible covers exist, so the live sunrise/sunset cover window has
  no target.

Consequently the occupied welcome, hot-entry boost/settle, cool-entry skip,
15-minute failsafe, mid-boost exit, and cover sun-window branches remain
verified by the P2 deterministic adapter suite rather than by spoofing live
household sensors. A controllable P8 fixture room (virtual presence,
temperature, climate, light, and cover entities) is required for a meaningful
physical E2E of those branches.

## Cleanup

All three Energy configs were reset to `setup_complete=false`, Energy is
inactive with zero releases, and only the original admin remains paired. The
temporary dev manifest and JWT files were removed. CasaSmart emitted no startup
or runtime errors during the final recovery pass; unrelated pre-existing
`easy_ir` warnings remain outside this phase.

## Regression verification

- hub suite: 1,056 total — 906 passed and 150 expected environment-only skips;
- tablet suite after the release-listener fix: 1,067 tests passed with 32
  expected skips;
- Flutter analyzer and Python compilation: clean;
- source worktrees: hub and tablet contain only their intended committed P8
  changes, while the production mobile repository remains clean.
