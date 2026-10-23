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

Off rooms go in tiers; a room takes the first it qualifies for, and its `rank.reason` says which:

1. `running_load`: something still runs although nothing counted is on. `params.loads` lists what, in this order: `power` (one of the room's own meters, as above, reads at least 20 W; `rank.watts` has the room's total) and `media` (a media player is `playing`; `on`, `idle` and `paused` don't count, nor one with `casasmart_room_activity_exclude`). Higher watts first (10 % steps; no reading last). Open or moving covers and unmetered heater gangs never count.
2. `just_off`: the room went dark in the last 30 minutes. `params.went_dark_at` is when (ISO 8601, UTC); newest first. Taken from the hub's room activity log: the newest on/off of the devices that count the room as on must be an off made by a person (wall switch, Home Assistant user, another integration, or a member's app tap), not by an automation or a hub scene, and not by the member asking (their own app tap). Not just_off: a device that went dark through `unavailable`/`unknown`; a counted device that is unavailable or unknown and changed at or after the off; an off logged in the first 10 minutes after Home Assistant started (devices catching up); a device's old room after it moved.
3. `room_order`: `sort_order`, then `name`, then `room_id`.

Rooms that are on have `reason: "active"`. Within a tier, ties go to the room order. `params` never holds a member id or an entity id. The just_off tier depends on who asks, so two members can see the off rooms in a different order; everything else is the same for every member with the same scope. The generated room suggestions rank without the activity log (they only use rooms that are on).

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
- `reason`: `active`, `running_load`, `just_off` or `room_order` (above). Later versions may add reasons between `just_off` and `room_order`; show an unknown reason like `room_order`.
- `params`: `{}` for `active` and `room_order`; `{"loads": [...]}` for `running_load`; `{"went_dark_at": "..."}` for `just_off`.

`room_activity` gains `"ranking": {"version": 1, "basis": "watts" | "count", "slots": 5, "mode": "on" | "all_off"}`. Apps detect the ranking by this object.
