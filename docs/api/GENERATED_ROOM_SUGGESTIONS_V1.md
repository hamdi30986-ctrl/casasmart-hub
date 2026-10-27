# Generated room suggestions

The hub generates up to three temporary scenes without saved routines: turn off the most-active room, save energy in that room, and turn off the second-most-active room. A client that asks for it also gets up to two "turn on" scenes for rooms that are off but usually on at this hour (`usually_on_now`, below). Generation never executes commands or creates registry scenes.

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

Dismissal and snooze are member-scoped and survive plan changes within the same room/kind/window. Execution claims are global for that slot: concurrent clients, changed action hashes, partial failures, lost responses and restarts cannot run it again. Successful, partial and unknown attempts suppress the slot for the rest of its window. Snooze lasts at most 30 minutes. Registry and state changes trigger change events that carry no details; reads and timers never run scenes.

## Usually on around now (`usually_on_now`)

The additive `generated_room_habit_suggestions_v1` capability adds a third kind: a room that is off now but is usually switched on at this hour (`room_intel.habits`, the Now page's `habit_now`), with the devices that usually come on there.

### Asking for it

Both endpoints take two query parameters:

- `kinds`: a comma-separated list of the kinds the client shows, from `room_off`, `room_eco` and `usually_on_now`. Without it the client gets `room_off` and `room_eco` only, exactly as before this capability existed; that is how an older app keeps working. Names the hub doesn't know are skipped, so a newer app can ask for a later kind; an empty list, or one longer than 256 characters, is `400 invalid_kinds`.
- `habits`: `household` (the shared tablet: the whole household's habits, with no "you") or `personal` (the default: the caller's own app taps blended over the household's, as on the Now page). Anything else is `400 invalid_habits`.

`POST .../generated/actions` must carry the same `kinds` and `habits` as the `GET` that showed the occurrence: the hub regenerates the plan from them, and a different query (or none) answers `409 occurrence_expired_or_ineligible`.

### Which rooms and devices

Rooms are ranked as the Now page ranks them for this caller (activity log and habits included). A room is offered only when its tier is `habit_now`: nothing in it is on (no light, fan, wall relay or running AC), nothing is still running there, nobody switched it off in the last 30 minutes, and the caller's habit layer has learned it is usually on now (strength at least 0.35 on at least 3 dates, after two weeks of history). It must take part in room commands; a room an admin took out never gets one. At most two rooms are offered, the strongest habit first, then room order. None are offered while learning is off, while the habits are warming up, or while an Energy Saving level is active.

A device goes in the plan when its own votes pass the same bar at this hour (in the same habit layer), and it is one one-tap "on" may turn on: a light, a fan, or a wall relay the installer typed as a light or fan, approved by the room's policy, not left out of room activity, available and off. ACs, untyped wall relays, plugs, outlets and heaters are never turned on. A room with no such device gets no plan. Devices are listed strongest first, then by entity ID.

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
    {"entity_id": "light.majlis", "action": "turn_on", "data": {}, "name": "Majlis light"}
  ],
  "generated_at": "2026-10-22T16:10:00+00:00",
  "expires_at": "2026-10-22T18:00:00+00:00",
  "reason": {"code": "usually_on_now", "layer": "household", "hour": 19}
}
```

`reason.layer` is the habit layer that answered (`household` or `personal`); `reason.hour` is the local hour (0-23) the room is usually on at. Each action is a plain `turn_on`; `data` is empty for now and is where a level (a brightness, a fan speed) will go, so clients must send nothing themselves and should show `data` when it is not empty. Items come after the `room_off` and `room_eco` ones, so `suggestion` is a `usually_on_now` item only when nothing else is offered.

When `usually_on_now` was evaluated, `refresh_at` is the end of the two-hour window or the next local hour, whichever is sooner (a habit is an hour's). Otherwise it is the window's end, as before.

### Running, dismissing

Running, dismissing and snoozing work as for the other kinds: the same slot (room, kind, window) rules, member-scoped dismissals, and one global execution claim per slot. Each step is checked again as it runs: the device must still be in the room and off, so one switched on meanwhile is skipped ("Device changed since preview"). A `usually_on_now` scene never runs while Energy Saving is active (`409`), and is not offered then.

## Compatibility

This contract requires both the hub capability and an app that supports it. Without the capability, the app shows only rule-based suggestions. `usually_on_now` items appear only for a client that lists them in `kinds`; without that parameter the response is the same as before, byte for byte.
