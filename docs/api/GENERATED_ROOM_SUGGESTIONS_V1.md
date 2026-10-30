# Generated room suggestions

The hub generates up to three temporary scenes without saved routines: turn off the most-active room, save energy in that room, and turn off the second-most-active room. A client that asks for it also gets up to two "turn on" scenes for rooms that are off but usually on at this hour (`usually_on_now`, below), and up to two "turn off" scenes for what is on but usually switched off by now (`usually_off_now`, below), and can ask for those alone once the habits are ready (`habits_only`, below). Generation never executes commands or creates registry scenes.

## Contract

The additive `generated_room_suggestions_v1` capability enables `GET /api/casasmart/now/suggestions/generated` and `POST /api/casasmart/now/suggestions/generated/actions`. Existing rule-based endpoints remain unchanged. Generated reads require `devices.read`; dismissing or snoozing additionally requires `session.manage` (every role's session, not a widget token), and running additionally requires `devices.control`, with room scope and energy lockout enforced again before dispatch.

The version-one response contains `status`, `suggestions`, the first item as `suggestion` for the shared model, and `refresh_at`. Each item includes `source: generated_room_v1`, `kind: room_off|room_eco`, room ID/name, typed action previews, an opaque occurrence ID, and expiry. Actions accept only `action: run|dismiss|snooze` and `occurrence_id`; clients cannot submit arbitrary device commands or room IDs.

## Room selection and timing

The Now page and the generator use one hub ranking (`NOW_ROOM_RANKING_V1.md`): rooms with something on first, by watts when every one of them is fully metered, otherwise by how many devices are on (what room off switches: lights, fans and wall relays, typed or untyped; plus each running AC; never a plug, an outlet or a heater), with ties going to the room order (`sort_order`, name, room ID). The generator takes the first two rooms in that order whose `active_count` is above zero, so a room with only its AC on is not chosen; off rooms that fill the Now page's empty cards never are. For `room_off` and `room_eco` the generator ranks without the room activity log and without habits, so its off rooms are never `just_off` or `habit_now`; those kinds don't use off rooms. Only imported control entities participate; hidden gangs, configuration entities and unimported HA entities are excluded from both ranking and generated actions. Counts use the Now page's rule, including gang types that older device records key by channel suffix; the scene action allowlist (lights and ACs) differs. Explicit configured room policies constrain ranking; otherwise visible room activity is used. Scope is applied before ranking.

The selected two rooms persist for fixed two-hour UTC windows, including hub restarts. Vacant slots can fill when device states arrive after startup. Existing selections do not jump between rooms on every state update. The Now page's live ranking may still change within the window. Action previews always use current states and room assignments; irrelevant scenes disappear. Fewer than three suggestions is valid.

## Device actions

- Room off includes active `light` entities and active cooling-capable `climate` entities that support off. It excludes every plug, switch, fan, cover, lock, heating-only thermostat and unavailable device.
- Energy saving caps supported, currently bright lights at 128/255, never brightens dim lights, and switches off a stable entity-ID-ordered subset of non-dimmable lights. At least one currently active light is left on.
- AC adjustments apply only in cooling mode. A supported cooler target may increase to 24°C; a target already warmer than 24°C is never lowered. Low fan is requested only when supported. Fahrenheit uses the equivalent 75.2°F only when the advertised step and limits allow it. Temperature and fan commands both execute.
- Explicit room-activity exclusions are respected by generated actions. Device names are never used to decide whether a device is safe to switch.

## Execution and suppression

The app previews exact devices and actions, then requires a separate Run scene confirmation. The occurrence hash binds the previewed plan and window. The hub regenerates and rechecks it after acquiring a durable SQLite execution claim. Changed plans, revoked permissions, moved devices or expired windows fail closed. Each generated command also checks live state and room membership after preceding commands yield.

Dismissal and snooze are member-scoped and survive plan changes within the same room/kind/window. A member's dismissal or snooze of a room's `usually_off_now` also suppresses that room's `room_off` for them, for as long (same window, same `until`), whatever the client asked for; a `room_off` dismissal leaves the `usually_off_now`. Execution claims are global for that slot: concurrent clients, changed action hashes, partial failures, lost responses and restarts cannot run it again. Successful, partial and unknown attempts suppress the slot for the rest of its window. Snooze lasts at most 30 minutes. Registry and state changes trigger change events that carry no details; reads and timers never run scenes.

## Usually on around now (`usually_on_now`)

The additive `generated_room_habit_suggestions_v1` capability adds a third kind: a room that is off now but is usually switched on at this hour (`room_intel.habits`, the Now page's `habit_now`), with the devices that usually come on there, each at the level it was last used at this hour.

### Asking for it

Both endpoints take two query parameters:

- `kinds`: a comma-separated list of the kinds the client shows, from `room_off`, `room_eco`, `usually_on_now` and `usually_off_now`. Without it the client gets `room_off` and `room_eco` only, exactly as before this capability existed; that is how an older app keeps working. Names the hub doesn't know are skipped, so a newer app can ask for a later kind; an empty list, or one longer than 256 characters, is `400 invalid_kinds`.
- `habits`: `household` (the shared tablet: the whole household's habits, with no "you") or `personal` (the default: the caller's own app taps blended over the household's, as on the Now page). Anything else is `400 invalid_habits`.

`POST .../generated/actions` must carry the same `kinds` and `habits` as the `GET` that showed the occurrence: the hub regenerates the plan from them, and a different query (or none) answers `409 occurrence_expired_or_ineligible`.

### Which rooms and devices

Rooms are ranked as the Now page ranks them for this caller (activity log and habits included). A room is offered only when its tier is `habit_now`: nothing in it is on (no light, fan, wall relay or running AC), nothing is still running there, nobody switched it off in the last 30 minutes, and the caller's habit layer has learned it is usually on now (strength at least 0.35 on at least 3 dates, after two weeks of history). It must take part in room commands; a room an admin took out never gets one. At most two rooms are offered, the strongest habit first, then room order. None are offered while learning is off, while the habits are warming up, or while an Energy Saving level is active.

A device goes in the plan when its own votes pass the same bar at this hour (in the same habit layer), it is available, off and not left out of room activity, and it is either one one-tap "on" may turn on (a light, a fan, or a wall relay the installer typed as a light or fan, approved by the room's policy) or an AC (a `climate` entity with `cool` in its `hvac_modes`, in any room that takes part, as an AC counts in every room). An AC goes in only with a recorded mode and target it still takes (below). Untyped wall relays, plugs, outlets, heaters and heating-only thermostats are never turned on. A room with no such device gets no plan. Devices are listed strongest first, then by entity ID; an AC's steps stay together.

### Levels

Each device comes back at the level it was last used at this hour, not an average. Since hub schema v6 the room activity log records, with each on (and an AC's mode change), the level the device was at: a light's `brightness` (1-255) and, in colour temperature mode, `color_temp_kelvin`; a fan's `percentage`; an AC's `hvac_mode`, target `temperature` and `fan_mode`. A person changing the level of a device that stays on (dimming a light, an AC's setpoint or fan) is logged too; the device's own report within 30 seconds of a switch-on (a light ramping up, an AC settling) belongs to that switch-on. Levels set by automations and unnamed callers never count as used.

The level used is the device's newest one in this room at this local hour, on a day like today (workday or weekend, as the habits count them), within today's habit window (Ramadan's during Ramadan) or today; with none at this hour, the newest an hour either side. Dates learning was off or paused, dates before a reset and a self-scheduled device's wall-switch rows don't count. It is the household's last use whichever layer answered, so a phone and the tablet restore the same level. A device with no recorded level (all its uses were logged before v6) gets a plain on.

Only what the device still supports goes in: brightness for a dimmable light (a `supported_color_modes` other than `onoff`), a colour temperature only with `color_temp` in its modes and inside `min_color_temp_kelvin`..`max_color_temp_kelvin`, a speed only for a fan that supports one. An AC needs its recorded mode in its `hvac_modes`, target-temperature support and the target inside `min_temp`..`max_temp` (the target is in Home Assistant's unit, as recorded); its fan mode is added only when it is still in `fan_modes`. An AC that fails any of these is left out of the plan rather than turned on at a guess.

### The item

Same envelope as the other kinds:

```json
{
  "source": "generated_room_v1",
  "kind": "usually_on_now",
  "room_id": "room-majlis",
  "room_name": "Majlis",
  "rule_id": "<64 hex: room + kind + window>",
  "suppression_id": "<same as rule_id>",
  "occurrence_id": "<64 hex: also covers the actions>",
  "scene_id": "<same as occurrence_id>",
  "scene": {"scene_id": "<occurrence_id>", "name": "Turn on · Majlis", "icon": "lightbulb"},
  "actions": [
    {"entity_id": "climate.majlis_ac", "action": "set_hvac_mode", "data": {"hvac_mode": "cool"}, "name": "Majlis AC"},
    {"entity_id": "climate.majlis_ac", "action": "set_temperature", "data": {"temperature": 23.0}, "name": "Majlis AC"},
    {"entity_id": "climate.majlis_ac", "action": "set_fan_mode", "data": {"fan_mode": "low"}, "name": "Majlis AC"},
    {"entity_id": "light.majlis", "action": "turn_on", "data": {"brightness": 204, "color_temp_kelvin": 2700}, "name": "Majlis light"},
    {"entity_id": "switch.majlis_lamp", "action": "turn_on", "data": {}, "name": "Majlis lamp"}
  ],
  "generated_at": "2026-10-22T16:10:00+00:00",
  "expires_at": "2026-10-22T18:00:00+00:00",
  "reason": {"code": "usually_on_now", "layer": "household", "hour": 19}
}
```

`reason.layer` is the habit layer that answered (`household` or `personal`); `reason.hour` is the local hour (0-23) the room is usually on at. A light, fan or wall relay is one `turn_on`, with its level in `data` (`brightness`, `color_temp_kelvin`, `percentage`) or `data: {}` for a plain on. An AC is `set_hvac_mode`, `set_temperature` and, when it had one, `set_fan_mode`, in that order, each with its one value. Clients send nothing of this themselves; they should show the levels (and an AC's mode and target) in the preview. A level change makes a new `occurrence_id` (same `suppression_id`), so a plan shown before someone used the room again can't run as something else. Items come after the `room_off` and `room_eco` ones, so `suggestion` is a `usually_on_now` item only when nothing else is offered.

When `usually_on_now` was evaluated, `refresh_at` is the end of the two-hour window or the next local hour, whichever is sooner (a habit is an hour's). Otherwise it is the window's end, as before.

### Running, dismissing

Running, dismissing and snoozing work as for the other kinds: the same slot (room, kind, window) rules, member-scoped dismissals, and one global execution claim per slot. Each step is checked again as it runs: the device must still be in the room and off, so one switched on meanwhile is skipped ("Device changed since preview"). An AC's mode step needs it still off; its target and fan steps need it off (a cloud AC may report its new mode late) or in the plan's mode, so an AC someone set to something else meanwhile keeps their setting. Like a saved scene, the run sends an AC's mode and target but skips its fan step (an IR AC takes each call as a full-state burst and garbles a third one right after the mode); the item still shows the fan mode last used, and the receipt counts only the steps sent. Only these steps, with only these data keys, can run in a `usually_on_now` scene; each also passes the single-device command whitelist (no `climate.turn_on`). A `usually_on_now` scene never runs while Energy Saving is active (`409`), and is not offered then.

## Usually off by now (`usually_off_now`)

The additive `generated_room_habit_off_suggestions_v1` capability adds a fourth kind: a room that is on, with its devices that are on but are usually switched off by now. It is asked for exactly like `usually_on_now`: `kinds` must list `usually_off_now`, `habits` picks the layer (`household` on the shared tablet), and `POST .../generated/actions` must carry the same query as the `GET` that showed it.

### Which rooms and devices

The habits learn each device's offs from the room activity log as they learn its ons: the same sources and weights (an app tap, a scene a member ran, a Home Assistant user and a wall switch weigh 1, a wall switch only in the household layer; automations, Energy Saving and unnamed callers weigh nothing; a self-scheduled device's wall-switch offs are ignored), the same layers, window, decay, day class and Ramadan profile. An off never votes for its room, so the Now page's ranking and `usually_on_now` don't change. For each date, a device counts as off by now at local hour h when it was switched off at h or at h−1 and not switched on at h (on and off within the same hour is a device in use, such as a bathroom light). A device is usually off by now at the same bar as a room is usually on: strength at least 0.35 on at least 3 dates, after two weeks of history. So the suggestion starts at the usual hour and lasts until the end of the next one, and never comes before the usual time.

A room is offered when it is on and takes part in room commands. Its plan holds its devices that are on now and usually off by now, strongest first, then by entity ID, and only what makes the room on on the Now page (`NOW_ROOM_RANKING_V1.md`): the lights, fans and wall relays (typed as a light or fan, or untyped) that room off switches under the room's policy, and its ACs (a `climate` entity with `cool` in its `hvac_modes`) that run in a mode and have `off` in their `hvac_modes`. Never a plug, an outlet, a fridge or anything typed as an outlet, plug or heater, a lock, an alarm, a cover, a TV, a heating-only thermostat or a device left out of room activity, whatever the habits say. A device switched on (or into another mode) less than 30 minutes ago is in use and left out until then; `refresh_at` is that moment when it comes before the next local hour. At most two rooms are offered, in the ranking's order (busiest first). A room can have both a `room_off` and a `usually_off_now` item: `room_off` turns off its lights and ACs, `usually_off_now` only what is usually off by now.

The thresholds are those of `usually_on_now` on purpose, and follow how assistants that do this decide: Alexa's Hunches flagged a device "not in its usual state" (a light left on at bedtime) only after a few weeks of learning, and only asked; Google Home's suggested automations are offered for the user to accept or dismiss. Here nothing runs without a tap, the hub learns for two weeks first, a habit needs most of the recent days, and a dismissal holds for the window.

### The item

```json
{
  "source": "generated_room_v1",
  "kind": "usually_off_now",
  "room_id": "room-majlis",
  "room_name": "Majlis",
  "rule_id": "<64 hex: room + kind + window>",
  "suppression_id": "<same as rule_id>",
  "occurrence_id": "<64 hex: also covers the actions>",
  "scene_id": "<same as occurrence_id>",
  "scene": {"scene_id": "<occurrence_id>", "name": "Turn off · Majlis", "icon": "power"},
  "actions": [
    {"entity_id": "climate.majlis_ac", "action": "turn_off", "data": {}, "name": "Majlis AC"},
    {"entity_id": "light.majlis", "action": "turn_off", "data": {}, "name": "Majlis light"},
    {"entity_id": "switch.majlis_lamp", "action": "turn_off", "data": {}, "name": "Majlis lamp"}
  ],
  "generated_at": "2026-10-22T20:10:00+00:00",
  "expires_at": "2026-10-22T22:00:00+00:00",
  "reason": {"code": "usually_off_now", "layer": "household", "hour": 23}
}
```

Every action is a plain `turn_off` with empty `data`. `reason.layer` is the habit layer that answered; `reason.hour` is the local hour it is now. Items come after the `usually_on_now` ones. When `usually_off_now` was evaluated, `refresh_at` is the window's end, the next local hour or the moment a device stops being "just switched on", whichever is soonest.

### Running, dismissing

As for the other kinds: the same slot rules, member-scoped dismissals and snoozes, one global execution claim per slot. Each step is checked again as it runs: the device must still be in the room and on (an AC in a mode with `off`), the step a plain `turn_off`, and the device not left out of room activity; one switched off or moved meanwhile is skipped ("Device changed since preview"). Turning off saves energy, so a `usually_off_now` item is offered and runs while Energy Saving is active (an energy lockout still refuses it).

## Habits only, once learned (`habits_only`)

The additive `generated_room_habits_only_v1` capability adds a third query parameter to both endpoints:

- `habits_only`: `1` or `0` (the default). With `1`, once the caller's habits are ready the hub answers only the habit kinds asked for (`usually_on_now`, `usually_off_now`), and before then only the other kinds asked for (`room_off`, `room_eco`), as the cold-start fallback of a new home. The two never mix in one answer. Anything else is `400 invalid_habits_only`.

The answer then carries `"habits_ready": true|false`, which side it took. An answer without `habits_only=1` has no such field and is unchanged. `POST .../generated/actions` must carry the same query as the `GET`: a `room_off` a ready caller no longer gets answers `409 occurrence_expired_or_ineligible`.

### When the habits are ready

The rule is the habits' own bar, applied to every hour of the day instead of only now: the caller's habit layer is past the warm-up (14 observed dates), and at some local hour of today's day class (workday or weekend; Ramadan's profile during Ramadan) at least one device passes it, strength at least 0.35 on at least 3 dates, either for coming on (as `usually_on_now` reads) or for being off by then (as `usually_off_now` reads). Only devices a habit plan may switch count, in rooms that take part in room commands: for coming on, the lights and fans one tap "on" may turn on and the ACs; for being off, what makes the room on (lights, fans, typed or untyped wall relays, ACs). So a habit only an untyped wall relay's switch-on has, or one in a room an admin took out, doesn't make a home ready.

- **All day, not hour by hour.** A ready home stays ready at hours with nothing usual: the answer is then empty (`no_match`), not the room plans. The habits are rebuilt once a day, so the answer can change only at local midnight, when the day class changes, or when an admin turns learning off, resets it or changes the devices.
- **Per caller.** It is computed in the layer that builds the caller's plans: the household's for `habits=household` (the shared tablet), the member's own taps blended over the household's for a phone. A phone whose blend can't build a habit plan keeps the fallback rather than an empty row, even when the tablet's household habits are ready. Within the caller's room scope.
- **Not ready** while learning is off, the habits are warming up or can't be read, or on a hub without the habit learner: the fallback.

Energy Saving doesn't change readiness: a ready caller then gets only its `usually_off_now` plans.

## Compatibility

This contract requires both the hub capability and an app that supports it. Without the capability, the app shows only rule-based suggestions. `usually_on_now` and `usually_off_now` items appear only for a client that lists them in `kinds`; without that parameter the response is the same as before, byte for byte, and an app that lists only `usually_on_now` gets what it got before `usually_off_now` existed. Without `habits_only=1` the kinds asked for are all offered together, as before.
