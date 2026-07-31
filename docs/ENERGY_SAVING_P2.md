# Energy Saving P2 — Home Assistant automation adapter

Date: 2026-07-31

Canonical specification: `/Users/macmini/ENERGY_SAVING_PLAN.md` (FINAL SPEC v4)

## Boundary

P2 implements the Home Assistant adapter that executes the durable P1 engine
contract:

- point-in-time inventory from HA state plus CasaSmart room/gang records;
- deterministic Low, Medium, and Smart activation/re-apply rules;
- Smart occupancy, empty grace, comfort/energy postures, and AC boost/settle;
- Medium temperature edges and the Smart boost-settle temperature edge;
- sunrise/sunset cover behavior and night-only welcome lights;
- own-command tracking and per-device external-change release;
- room/device failure isolation and in-memory issue reporting.

P2 does **not** register REST/WS views, enforce API roles or lockout, disable HA
automations, expose discovery, create the HA energy sensor, alter factory reset,
or wire itself into integration setup. Those are P3 responsibilities. The
adapter is not imported by `__init__.py`, so committing P2 alone cannot run any
rule on a live home.

No P2 code was deployed to Home Assistant and no tablet/mobile file was changed.

## Inventory model

Registry assignments are authoritative, with HA entity/device area as the
fallback. The adapter separates:

- explicit gang records from ordinary grouped device records;
- real `light.*` entities from gang-backed lights;
- ambient temperature/presence sensors from config/diagnostic siblings;
- Smart automatic rooms (both temperature and presence) from sensorless or
  partial-sensor rooms.

Temperature fallback matching is deliberately narrow. Entity-registry
`config`/`diagnostic` records, `*_device_temperature`, battery, link-quality,
humidity, and similarly named sibling entities cannot become the room control
sensor.

The event-loop hot path uses in-memory entity/sensor maps. Full inventory is
rebuilt only for activation/re-apply and relevant occupancy, temperature, or
sun edges; there is no polling loop.

## Implemented rule matrix

### Static activation / re-apply

- Gangs: all channels must be on; Low keeps two of three; Medium/Smart keep one
  of two or three; one- and four-gang records are never managed.
- Low climate: active cooling targets below 24 °C are capped at 24; active
  heating targets above 21 °C are capped at 21; fan and off ACs are untouched.
- Medium climate: multi-AC keeper enforcement, 24 °C cooling / 21 °C heating,
  low fan when supported, and room-temperature kill guards.
- Smart sensorless climate: configured keep/off selection only; no
  resurrection or thermostat rewrite.
- Real lights: exact `ceil(n/2)` keeper fail-safe, unpicked lights off, keeper
  brightness capped down only at 80/60/40 percent, and off lights untouched.
- Configured plugs and heaters: off-only behavior; never resurrected.
- Covers: Low untouched; Medium and Smart sensorless rooms close only inside
  the heat window.

Excluded rooms and sensor-troubled rooms are skipped across every category.
Missing/stale candidates produce issues rather than guessing.

### Smart automatic rooms

- Presence is instant; empty has one cancellable 45-second timer per room.
- Empty confirmation clears that room's releases, cancels boost, turns AC and
  lights off, turns configured room plugs off, and heat-blocks covers only
  inside the day window.
- Occupied cooling: below 22 °C off; above 28 °C boost to 16/max; otherwise
  24/low.
- Occupied heating mirror: above 24 °C off; below 18 °C boost to 30/max;
  otherwise 21/low.
- Boost settles on the room sensor threshold or the exact 15-minute failsafe.
- A room exit cancels an active boost before the empty posture.
- Welcome keepers turn on at 60 percent only when the sun is down.
- Occupied covers open by day and close by night, including sun state edges.
- Sensor unavailability cancels dynamic timers, skips the room, records unknown
  occupancy, and exposes an issue for P3.

### Sun behavior

The HA `sun.sun` entity is the sole clock:

- day/night drives Smart comfort covers and welcome-light darkness;
- one hour after sunrise starts the heat-block window;
- one hour before sunset ends eligibility without issuing a command;
- activation/re-apply checks the current window;
- one exact cancellable timer is kept for the next window start.

## Release and failure safety

Every hub-internal device service call is recorded before execution. The own
command window is 15 seconds for lights/switches/climate and 120 seconds for
moving covers. State changes outside that ledger release only the changed
managed entity. Dynamic rules honor releases; the Smart room-empty exception
clears room releases first and then applies the energy posture.

Availability transitions do not masquerade as manual overrides. One failed HA
service call is logged and reported, while remaining rooms/devices continue.
Unsupported domains and stale picker data fail closed.

The known IR limitation remains: rules send climate commands best-effort, but a
write-only IR device cannot report remote overrides for release detection.

## P3 lifecycle seam

P3 should wire the adapter in this order:

1. construct `EnergyAdapter(hass, energy_engine, registry_engine)`;
2. call `async_start()` once after engine warm-up;
3. after successful engine activation or re-apply, await
   `async_apply(reason=...)`;
4. after engine deactivation, call `async_mode_stopped()`;
5. call `async_stop()` on unload;
6. merge `issues()` into the authenticated Energy state response.

Engine activation/re-apply must happen before adapter application so activation
starts with an empty release set and re-apply clears releases before commands.

## Pre-P2 live inventory gate

The previous documented dev endpoint, `192.168.8.235:8124`, was offline on
2026-07-31. Local Home Assistant discovery found a responding instance at
`192.168.8.61:8123`; its public CasaSmart health response reported hub `1.1.0`
and schema v3. That instance rejected the saved `.dev` identity with
`404 Unknown device`.

No new identity was paired/enrolled and no device/registry command endpoint was
called. Therefore the live state/capability sweep remains incomplete and must
be repeated before P3 is deployed.

The verified pre-Eco runtime backup was inspected read-only as a secondary
shape check. It contains 344 assigned entities across 9 rooms, including 6
climates, 33 lights, 79 switches, 130 sensors, and 8 binary sensors. It shows
temperature/presence candidates but cannot prove current availability,
dimming, fan modes, or gang layouts. It also exposed diagnostic temperature
siblings, which directly resulted in the stricter inventory classifier and its
regression test.

## Verification

Automated:

- focused P2 adapter suite: 28 passed;
- focused P1 + P2 engine/adapter suite: 72 passed;
- full hub suite: 1,038 passed, 145 expected skips;
- Python compile, `tabnanny`, and `git diff --check`: passed.

Coverage includes the complete static matrix, all-on gang gating, exact light
keepers, no resurrection, cooling/heating guards, sensorless Smart behavior,
instant presence, empty cancellation/expiry, boost threshold/failsafe/exit,
night welcome, sun-window start and sunset, self-expiring releases,
own-command suppression, manual release without static re-evaluation,
one-gang exclusion, unavailable sensors, diagnostic-sensor exclusion, command
failure isolation, sensor recovery, and lifecycle timer cancellation.
