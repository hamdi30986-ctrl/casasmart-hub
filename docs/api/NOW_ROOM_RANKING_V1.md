# Now page room ranking

`GET /api/casasmart/now` lists the caller's rooms in `room_activity.rooms`, the rooms with something on first. The generated room suggestions use the same ranking (`GENERATED_ROOM_SUGGESTIONS_V1.md`). Everything here is additive: no earlier field was removed, renamed or changed in meaning, and apps that don't read the new fields see the same shapes as before.

## What counts as on

A room's on devices are its safe controls that are on (lights, fans, and switches whose gang is a light, fan, switch or outlet; this is `active_count`, unchanged) plus every AC that is on. An AC is an imported `climate` entity with `cool` in its `hvac_modes` and no `casasmart_room_activity_exclude`; it is on in `cool`, `heat`, `auto`, `heat_cool`, `dry` or `fan_only`. Each running AC is one device. ACs count in homes with room policies too, since a policy can't approve one. Only devices imported into the registry count, and scope is applied first.

## Watts or count

A room's power comes from its own devices' meters: a `sensor` with `device_class: power` on an imported device that has a counted control or AC in that room, and whose own room is that room. A standalone meter (a CT clamp or whole-home meter, whose device has no light, fan, switch or AC) never joins a room. A reading counts when it is available, numeric and in a power unit (mW, W, kW, MW, GW, TW, BTU/h, converted to W). Below −2 W it is invalid (wrong polarity; never made positive); between −2 W and 0 it reads 0. Reading age is not judged: Home Assistant writes a steady MQTT reading only when it changes.

An on device is covered when its device has a meter in the room and every one of them reads valid. `watts_quality` is `absent` with no valid reading, `complete` when every on device is covered, else `partial`.

When at least one room is on and every on room is `complete`, the on rooms rank by watts (`ranking.basis: "watts"`). Otherwise the whole list ranks by how many devices are on (`"count"`).

## Order and ties

- Count: more devices on first.
- Watts: higher watts first, compared in 10 % steps so meter noise doesn't swap two rooms on every refresh (readings more than 10 % apart always order by watts); within a step, more devices on first.
- Then the room order the registry serves the apps: `sort_order`, then `name`, then `room_id`.

## Filling empty slots

The Now page has five room cards (a featured one and four more, as in `layout`). When fewer than five rooms are on, off rooms fill the empty cards, in the all-off order below. Only a room with a device to switch (an available counted control or AC, on or off) or something still running fills one. In a home with room policies every participating room is still listed: the rooms that are on, then the fillers, then the other participating rooms in the same order. Without policies only the on rooms and the fillers are listed. When no room in the caller's scope is on, `ranking.mode` is `all_off` and every listed room is in the all-off order.

## The all-off order

Off rooms go in tiers; a room takes the first it qualifies for, and its `rank.reason` says which (the viewer's own off, 4, is checked before 3 but sorts after it):

1. `running_load`: something still runs although nothing counted is on. `params.loads` lists what, in this order: `power` (one of the room's own meters, as above, reads at least 20 W; `rank.watts` has the room's total) and `media` (a media player is `playing`; `on`, `idle` and `paused` don't count, nor one with `casasmart_room_activity_exclude`). Higher watts first (10 % steps; no reading last). Open or moving covers and unmetered heater gangs never count.
2. `just_off`: the room went dark in the last 30 minutes. `params.went_dark_at` is when (ISO 8601, UTC); newest first. Taken from the hub's room activity log: the newest on/off of the devices that count the room as on must be an off made by a person (wall switch, Home Assistant user, another integration, or a member's app tap), not by an automation or a hub scene. Not just_off: a device that went dark through `unavailable`/`unknown`; a counted device that is unavailable or unknown and changed at or after the off; an off logged in the first 10 minutes after Home Assistant started (devices catching up); a device's old room after it moved.
3. `habit_now`: the room is usually switched on around this hour (label "Usually on around now"); strongest first. See "Habits" below.
4. `just_off` with `params.by_you: true`: the member asking switched it off from the app in the last 30 minutes (label "You turned this off"); newest first. Same rules as 2. Such a room is never `habit_now`.
5. `room_order`: `sort_order`, then `name`, then `room_id`.

Rooms that are on have `reason: "active"`. Within a tier, ties go to the room order. `params` never holds a member id, an entity id or a score. The just_off tiers and the habits depend on who asks, so two members can see the off rooms in a different order; everything else is the same for every member with the same scope. The generated room suggestions rank without the activity log and without habits (they only use rooms that are on).

## Habits

The hub learns when each room is usually switched on from its room activity log (never Home Assistant's recorder). A confirmed "on" of a device that makes the room on (as above: a light, a fan, a light/fan/switch/outlet switch, an AC) votes for that room, local date and local hour, once per hour however often it was tapped. An app tap, a scene a member ran, a Home Assistant user and a wall switch weigh 1, but a wall switch (no person behind it) votes only for the household, never in a member's own layer; automations and unnamed callers weigh nothing; a device an admin marked as running on its own schedule has its wall-switch events ignored.

For the current hour and day class (workday, or weekend: Friday and Saturday unless an admin changes it), over the last 56 days (today left out): each date's vote at that hour, or half a vote an hour either side (across midnight too), weighted by `2^(−age/14 days)`, divided by the same weights plus 4 empty days. A room is `habit_now` at 0.35 or more with evidence on at least 3 dates. A date counts only when someone did something by hand in one of the caller's rooms that day, so days away don't count against a habit and a scoped member's answer never depends on rooms they can't see. Nothing ranks on a habit until the caller's rooms have 14 such dates.

Whose habits: `GET /api/casasmart/now?habits=household` (the shared tablet) ranks on the household's. Without the parameter, or with `habits=personal`, the caller's own app taps are blended over the household's: `λ·own + (1−λ)·household`, `λ = min(0.8, N/(N+10))`, N = dates the caller switched something on from the app in their rooms. Any other value is a 400.

### Habit settings (admin)

Capability `now_room_habits_v1`. All three need `users.manage` (admin only).

- `GET /api/casasmart/now/habits` → `{"learning": true, "paused": false, "status": "learning", "weekend_days": [4, 5], "self_scheduled_entity_ids": [], "reset_at": null, "observed_days": 31, "warm_up_days": 14}`. `status` is `off`, `paused`, `warming_up`, `learning` or `unavailable`; `observed_days` counts the whole home's (null while learning is off); `reset_at` is ISO 8601 UTC or null.
- `PUT /api/casasmart/now/habits` with any of: `learning` (bool; off shows no habits and keeps no learned model, and the days while off are never learned), `paused` (bool; keeps ranking on what was learned, and the paused days are never learned), `weekend_days` (weekdays, Monday = 0 … Sunday = 6; applies at once), `self_scheduled_entity_ids` (at most 256 devices the hub manages that run on their own schedule: their wall-switch events stop counting). Answers like GET. An unknown field or a bad value is a 400 and changes nothing.
- `POST /api/casasmart/now/habits/reset` forgets every learned habit: nothing before the reset counts and learning starts over the next day, warm-up included. The activity log itself is kept. Answers like GET.

A member's own layer, and their share of the household's, is deleted with their last device (both leave paths), with their activity log rows and their recent devices.

## Fields

Each room gains, after its existing fields:

```json
"rank": {
  "active": true,
  "filler": false,
  "on_count": 3,
  "ac_on_count": 1,
  "watts": 1840.5,
  "watts_quality": "complete",
  "reason": "active",
  "params": {}
}
```

- `active`: something counted is on (`on_count > 0`).
- `filler`: an off room placed in one of the five cards to fill it; show it dimmed.
- `on_count`: `active_count` plus `ac_on_count`.
- `watts`: the room's valid meter readings summed, in W, one decimal; `null` when none.
- `reason`: `active`, `running_load`, `just_off`, `habit_now` or `room_order` (above). Later versions may add reasons between `just_off` and `room_order`; show an unknown reason like `room_order`.
- `params`: `{}` for `active`, `habit_now` and `room_order`; `{"loads": [...]}` for `running_load`; `{"went_dark_at": "..."}` for `just_off`, plus `"by_you": true` when the caller switched it off.

`room_activity` gains `"ranking": {"version": 1, "basis": "watts" | "count", "slots": 5, "mode": "on" | "all_off", "habits": {"layer": "household" | "personal", "status": "off" | "paused" | "warming_up" | "learning" | "unavailable"}}`. Apps detect the ranking by this object. `habits.status`: `off` (an admin turned learning off), `paused` (ranking on what was learned before the pause), `warming_up` (fewer than 14 dates yet), `learning`, `unavailable` (the hub couldn't read its log). Label the section "Rooms", not "Suggested", unless the status is `learning` or `paused`.
