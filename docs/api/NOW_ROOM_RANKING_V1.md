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

1. `running_load`: something still runs although nothing counted is on. `params.loads` lists what, in this order: `power` (one of the room's own meters, as above, reads at least 20 W; once it has, it keeps counting until it reads below 14 W or has no valid reading, so a device idling around 20 W doesn't flip in and out; the hub follows the readings in memory, so a restart starts this over; `rank.watts` has the room's total) and `media` (a media player is `playing`; `on`, `idle` and `paused` don't count, nor one with `casasmart_room_activity_exclude`). Higher watts first (10 % steps; no reading last). Open or moving covers and unmetered heater gangs never count.
2. `just_off`: the room went dark in the last 30 minutes. `params.went_dark_at` is when (ISO 8601, UTC); newest first. Taken from the hub's room activity log: the newest on/off of the devices that count the room as on must be an off made by a person (wall switch, Home Assistant user, another integration, or a member's app tap), not by an automation or a hub scene. Not just_off: a device that went dark through `unavailable`/`unknown`; a counted device that is unavailable or unknown and changed at or after the off; an off in the first 10 minutes after Home Assistant started (devices catching up; the log records nothing then); a device's old room after it moved.
3. `habit_now`: the room is usually switched on around this hour (label "Usually on around now"); strongest first. See "Habits" below.
4. `just_off` with `params.by_you: true`: the member asking switched it off from the app in the last 30 minutes (label "You turned this off"); newest first. Same rules as 2. Such a room is never `habit_now`. Never with `?habits=household` (a shared screen has no "you"): there the member's own offs are plain `just_off` (tier 2).
5. `room_order`: `sort_order`, then `name`, then `room_id`.

Rooms that are on have `reason: "active"`. Within a tier, ties go to the room order. `params` never holds a member id, an entity id or a score. The just_off tiers and the habits depend on who asks, so two members can see the off rooms in a different order; everything else is the same for every member with the same scope. The generated room suggestions rank without the activity log and without habits (they only use rooms that are on).

## Habits

The hub learns when each room is usually switched on from its room activity log (never Home Assistant's recorder). A confirmed "on" of a device that makes the room on (as above: a light, a fan, a light/fan/switch/outlet switch, an AC) votes for that room, local date and local hour, once per hour however often it was tapped. An app tap, a scene a member ran, a Home Assistant user and a wall switch weigh 1, but a wall switch (no person behind it) votes only for the household, never in a member's own layer; automations and unnamed callers weigh nothing; a device an admin marked as running on its own schedule has its wall-switch events ignored. Nothing is logged in the first 10 minutes after Home Assistant starts, so a device reporting its real state late after a restart never votes.

For the current hour and day class (workday, or weekend: Friday and Saturday unless an admin changes it), over the last 56 days (today left out): each date's vote at that hour, or half a vote an hour either side (across midnight too), weighted by `2^(−age/14 days)`, divided by the same weights plus 4 empty days. A room is `habit_now` at 0.35 or more with evidence on at least 3 dates. A date counts only when someone did something by hand in one of the caller's rooms that day, so days away don't count against a habit and a scoped member's answer never depends on rooms they can't see. Nothing ranks on a habit until the caller's rooms have 14 such dates.

### Ramadan

The hub detects Ramadan by itself (no switch): the dates of Ramadan by Saudi Arabia's Umm al-Qura calendar, from a table built into the hub (1440–1500 AH, 2019–2077; no internet needed). They are a profile of their own, learned apart from the rest of the year. Each profile has its own window and clock: its last 56 dates before today, aged in its own dates only. So during Ramadan the regular habits neither learn nor fade, and on 1 Shawwal they are exactly what they were on 1 Ramadan; and the Ramadan habits don't fade between one Ramadan and the next, so next year's first day starts from this one (newer Ramadan days then take over, like any changed habit). While it is Ramadan the ranking uses the Ramadan profile (same math, same 14-date warm-up in a home's first Ramadan), otherwise the regular one. A Ramadan window reaches back to last year's Ramadan, never the one before. Each finished Ramadan is copied out of the activity log the day after it ends, whether learning is on or not, and read only in the next Ramadan, so the log's row cap can't take it; the copy is deleted once the next Ramadan ends, so no one's activity is kept or learned from for longer than the log's 400 days. Pauses, learning off and the schedule opt-out apply in both profiles. A reset and a factory reset delete the kept Ramadans; turning learning off keeps them (as it keeps the log); a member's rows go with their last device.

Whose habits: `GET /api/casasmart/now?habits=household` (the shared tablet) ranks on the household's. Without the parameter, or with `habits=personal`, the caller's own app taps are blended over the household's: `λ·own + (1−λ)·household`, `λ = min(0.8, N/(N+10))`, N = dates the caller switched something on from the app in their rooms. Any other value is a 400.

### Habit settings (admin)

Capability `now_room_habits_v1`. All three need `users.manage` (admin only).

- `GET /api/casasmart/now/habits` → `{"learning": true, "paused": false, "status": "learning", "weekend_days": [4, 5], "self_scheduled_entity_ids": [], "reset_at": null, "observed_days": 31, "warm_up_days": 14, "profile": "regular", "ramadan": {"year": 1448, "first_day": "2027-02-08", "last_day": "2027-03-08", "kept_years": [1447]}}`. `status` is `off`, `paused`, `warming_up`, `learning` or `unavailable`; `observed_days` counts the whole home's, in today's profile (null while learning is off); `reset_at` is ISO 8601 UTC or null. `profile` is `regular` or `ramadan` (today's); `ramadan` is the current Ramadan, else the next (local dates, first and last day), with the Hijri years of the Ramadans the hub keeps (`kept_years` null if they couldn't be read); `ramadan` is null past the calendar table.
- `PUT /api/casasmart/now/habits` with any of: `learning` (bool; off shows no habits and keeps no learned model, and the days while off are never learned), `paused` (bool; keeps ranking on what was learned, and the paused days are never learned), `weekend_days` (weekdays, Monday = 0 … Sunday = 6; applies at once), `self_scheduled_entity_ids` (at most 256 devices the hub manages that run on their own schedule: their wall-switch events stop counting). Answers like GET. An unknown field or a bad value is a 400 and changes nothing.
- `POST /api/casasmart/now/habits/reset` forgets every learned habit, the kept Ramadans too: nothing before the reset counts and learning starts over the next day, warm-up included. The activity log itself is kept. Answers like GET.

A member's own layer, and their share of the household's (the kept Ramadans included), is deleted with their last device (both leave paths), with their activity log rows, their recent devices and the room-command answers kept for their retries (`now_idempotency`).

## One tap "on"

Capability `now_room_one_tap_v1`. What one tap does on a room that is off: the lights and fans that were on last time. Never a switch of any kind (relay, outlet, plug, heater), an AC, a cover, a TV or a water heater. The room command and the Now page ask the same question, so the card says what the tap does.

Only rooms that take part in room commands (their policy), and only the devices that policy approves, as for every room command. The first source with a light or fan of the room that is available wins:

1. `capture`: what the hub's own room off switched off, at most 24 hours ago, if the room hasn't been lit and switched off another way since (more than 2 minutes after the capture).
2. `last_on`: the lights and fans that were on just before the room last went dark, whoever switched it off (wall switch, Home Assistant, an automation, the hub): the light or fan whose off left nothing on, plus the room's others that went off in the 2 minutes before it. The hub follows this from Home Assistant's state changes; it starts 10 minutes after Home Assistant starts (at once on a reload), so a restart never replaces it. A room that went dark because its last light went unavailable, was removed or moved keeps the set it had. It holds no member id.
3. `start_profile`: the lights and fans an installer set for the room (below).
4. Nothing: the app opens the room.

Within the source, devices no longer in the room (deleted, moved, unapproved) are left out, and unavailable ones are skipped and reported. A source whose devices are all unavailable gives way to the next.

`POST /api/casasmart/now/rooms/{room_id}/activity` `{"action": "turn_on", "idempotency_key": "..."}` (same gates as before: `devices.control`, the room in scope, Energy Saving lockout, the room's policy):

- 200 `{"ok": true, "room_id": "...", "action": "turn_on", "outcomes": [{"entity_id": "light.majlis_main", "outcome": "changed"}, {"entity_id": "light.majlis_spot", "outcome": "unavailable"}], "restore_pending_count": 1, "restored_from_capture_count": 0, "source": "last_on", "restored_count": 1, "idempotency_key": "..."}`. Outcomes are `changed`, `failed` or `unavailable` (not sent a command). `ok` is true only when every planned device is on. `restored_count`: the plan's devices that are on now (switched on, or already on); `restored_from_capture_count` the same for a capture, else 0; `restore_pending_count`: the plan's devices still not on. With a capture, those stay captured for the next tap; a capture that is too old, outdated or has nothing of the room's left is dropped.
- 409 `{"error": "nothing_to_turn_on", "reason": "nothing_saved" | "unavailable", "message": "...", "open_room": true}`: nothing switched, nothing kept for a retry. Open the room; don't say "Couldn't turn on". (Replaces the unreleased `nothing_to_restore`.)
- `turn_off` is unchanged (it still switches every approved device that is on, and captures them).

### Start profile (installer)

`registry.manage` (admin and sub-admin), the room in the caller's scope (else 404):

- `GET /api/casasmart/now/rooms/{room_id}/start-profile` → `{"room_id": "...", "entity_ids": [...]}` (`[]` without one).
- `PUT` same path, `{"entity_ids": ["light.majlis_main", "fan.majlis"]}`: 1 to 64 of the room's lights and fans (served, in the room, not `casasmart_room_activity_exclude`). Anything else (a switch, outlet, AC, cover, TV, water heater, another room's light, a duplicate, an unknown field) is a 400 and nothing changes. Answers like GET.
- `DELETE` same path clears it; answers like GET.

Only devices the room's policy approves come on. A deleted room takes its profile and its last-on set with it. Last-on sets and start profiles are the room's, not a member's: a member leaving keeps them; a factory reset deletes them.

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
  "params": {},
  "one_tap": {"action": "turn_on", "source": "last_on", "light_count": 2, "fan_count": 1, "unavailable_count": 0}
}
```

- `active`: something counted is on (`on_count > 0`).
- `filler`: an off room placed in one of the five cards to fill it; show it dimmed.
- `on_count`: `active_count` plus `ac_on_count`.
- `watts`: the room's valid meter readings summed, in W, one decimal; `null` when none.
- `reason`: `active`, `running_load`, `just_off`, `habit_now` or `room_order` (above). Later versions may add reasons between `just_off` and `room_order`; show an unknown reason like `room_order`.
- `params`: `{}` for `active`, `habit_now` and `room_order`; `{"loads": [...]}` for `running_load`; `{"went_dark_at": "..."}` for `just_off`, plus `"by_you": true` when the caller switched it off.
- `one_tap` (`now_room_one_tap_v1`): what one tap "on" does, as above. `{"action": "turn_on", "source": "capture" | "last_on" | "start_profile", "light_count": 2, "fan_count": 0, "unavailable_count": 0}` (label e.g. "Restore 2 lights"; the counts are the available devices it covers), or `{"action": "open_room", "reason": "nothing_saved" | "unavailable" | "room_activity_disabled"}` (no ON button: the tap opens the room). A `running_load` card's tap should open the room too (no blind "on"), whatever `one_tap` says. Never an entity or member id.

`restore_pending_count` (an older field) is now the number of devices one tap "on" would turn on (`light_count + fan_count`, 0 for `open_room`), so apps that only read it enable an off room's switch exactly when the tap does something. It used to count the raw capture.

`room_activity` gains `"ranking": {"version": 1, "basis": "watts" | "count", "slots": 5, "mode": "on" | "all_off", "habits": {"layer": "household" | "personal", "status": "off" | "paused" | "warming_up" | "learning" | "unavailable", "profile": "regular" | "ramadan"}}`. Apps detect the ranking by this object. `habits.profile` (added with the Ramadan profile; absent from older hubs, read it as `regular`) says which profile answered. `habits.status`: `off` (an admin turned learning off), `paused` (ranking on what was learned before the pause), `warming_up` (fewer than 14 dates yet), `learning`, `unavailable` (the hub couldn't read its log). Label the section "Rooms", not "Suggested", unless the status is `learning` or `paused`.
