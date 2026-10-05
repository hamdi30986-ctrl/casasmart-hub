# Generated room suggestions

The hub generates up to three temporary scenes without saved routines: turn off the most-active room, save energy in that room, and turn off the second-most-active room. Generation never executes commands or creates registry scenes.

## Contract

The additive `generated_room_suggestions_v1` capability enables `GET /api/casasmart/now/suggestions/generated` and `POST /api/casasmart/now/suggestions/generated/actions`. Existing rule-based endpoints remain unchanged. Generated reads require `devices.read`; running additionally requires `devices.control`, with room scope and energy lockout enforced again before dispatch.

The version-one response contains `status`, `suggestions`, the first item as `suggestion` for the shared model, and `refresh_at`. Each item includes `source: generated_room_v1`, `kind: room_off|room_eco`, room ID/name, typed action previews, an opaque occurrence ID, and expiry. Actions accept only `action: run|dismiss|snooze` and `occurrence_id`; clients cannot submit arbitrary device commands or room IDs.

## Room selection and timing

NOW and the generator use one hub ranking: descending active count, then room ID. Only imported control entities participate; hidden gangs, configuration entities and unimported HA entities are excluded from both ranking and generated actions. Counts retain NOW's safe light/fan/classified-switch activity semantics, including legacy per-device suffix types; the scene action allowlist is narrower. Explicit configured room policies constrain ranking; otherwise visible room activity is used. Scope is applied before ranking.

The selected two rooms persist for fixed two-hour UTC windows, including hub restarts. Vacant slots can fill when device states arrive after startup. Existing selections do not jump between rooms on every state update. NOW's live big-card ranking may subsequently change within the window. Action previews always use current states and room assignments; irrelevant scenes disappear. Fewer than three suggestions is valid.

## Device actions

- Room off includes active `light` entities and active cooling-capable `climate` entities that support off. It excludes every plug, switch, fan, cover, lock, heating-only thermostat and unavailable device.
- Energy saving caps supported, currently bright lights at 128/255, never brightens dim lights, and switches off a stable entity-ID-ordered subset of non-dimmable lights. At least one currently active light is left on.
- AC adjustments apply only in cooling mode. A supported cooler target may increase to 24°C; a target already warmer than 24°C is never lowered. Low fan is requested only when supported. Fahrenheit uses the equivalent 75.2°F only when the advertised step and limits allow it. Temperature and fan commands both execute.
- Explicit room-activity exclusions are respected by generated actions. Names never establish appliance safety.

## Execution and suppression

The app previews exact devices and actions, then requires a separate Run scene confirmation. The occurrence hash binds the previewed plan and window. The hub regenerates and rechecks it after acquiring a durable SQLite execution claim. Changed plans, revoked permissions, moved devices or expired windows fail closed. Each generated command also checks live state and room membership after preceding commands yield.

Dismissal and snooze are member-scoped and survive plan changes within the same room/kind/window. Execution claims are global for that slot: concurrent clients, changed action hashes, partial failures, lost responses and restarts cannot blindly execute it again. Successful, partial and unknown attempts suppress the slot for the rest of its window. Snooze lasts at most 30 minutes. Registry/state changes trigger privacy-preserving invalidations; reads and timers never execute scenes.

## Rollout

This contract requires both the new hub capability and the matching app. Missing capability uses the existing manual-rule behavior. Source commits do not publish a HACS release or update a running Home Assistant instance. Physical AC behavior and energy reduction require supervised household acceptance; fixture tests do not establish measured savings.
