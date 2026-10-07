# Contextual suggestions API contract

The hub recommends existing CasaSmart scenes based on time and device state. Rules never execute automatically. An unrestricted administrator must explicitly enable them, and execution requires a separate authenticated Run request. This document is the contract between the hub and the app's rule editor and NOW card.

## Capability and endpoints

Use handshake feature `contextual_suggestions_v1`, version 1. A missing or unavailable capability means the editor and contextual actions are unsupported. Transport API version remains 1.

All paths below start with `/api/casasmart/now/suggestions`.

| Method and suffix | Permission | Purpose |
| --- | --- | --- |
| GET | `devices.read` | Current caller-scoped recommendation |
| GET `/rules` | `suggestions.manage` | Read rules and collection revision |
| PUT `/rules` | `suggestions.manage` | Replace rules with optimistic concurrency |
| POST `/preview` | `suggestions.manage` | Evaluate one proposed rule without saving or running it |
| POST `/actions` | `devices.read`, plus `session.manage` for Dismiss and Snooze or `devices.control` for Run | Dismiss, Snooze or Run a current occurrence |

`session.manage` is held by every role's session but not by a home-screen widget's token, so a widget can run a suggestion but not dismiss or snooze one. `suggestions.manage` is admin-only, not sub-admin. Management rejects room-scoped administrators because replacing the whole document must not overwrite unseen rules. Standard authentication, room scope and energy restrictions remain in force.

## Rules

GET `/rules` returns `{version: 1, revision: integer, rules: [...]}`. PUT requires `expected_revision` and the complete `rules` array. A stale revision returns HTTP 409 with `error: revision_conflict`; reload and reconcile explicitly rather than overwriting another administrator's changes. Removing a rule from the array deletes it. At most 64 rules are permitted.

Example PUT body:

```json
{
  "expected_revision": 0,
  "rules": [{
    "rule_id": "good-night",
    "scene_id": "scene-existing-id",
    "enabled": false,
    "priority": 0,
    "weekdays": [0, 1, 2, 3, 4, 5, 6],
    "window": {"kind": "fixed", "start": "23:00", "end": "06:00"},
    "conditions": [{"entity_id": "light.lounge", "state": "on"}],
    "match": "any"
  }]
}
```

Keep rule IDs stable across edits. IDs are 1–128 ASCII letters, digits, underscore, dot, colon or hyphen. The scene and every condition/action entity must exist and be served to the administrator. Unknown fields, duplicate rule IDs, duplicate condition entities and unsupported condition values are rejected. `enabled` defaults to false, `priority` to 0, `conditions` to an empty list and `match` to `all`. Priority is an integer from -100 to 100; higher wins, then ascending rule ID breaks ties.

Weekdays use Monday=0 through Sunday=6. Fixed windows use home-local `HH:MM`, include the start and exclude the end. An earlier end belongs to the following day; the start date owns the weekday. Equal endpoints are invalid. An empty condition list is time-only. There are at most 16 state-equality conditions. All/Any applies only to those explicit conditions; unknown/unavailable values never count as matches.

Supported condition domains and states are defined by `suggestions.STATES`: on/off for lights, switches, fans and binary sensors; open/closed for covers; locked/unlocked for locks; the listed HVAC modes for climate; and off/on/playing/paused/idle/standby for media players. No scripts, arbitrary expressions, numeric comparisons or inferred device/routine names are accepted.

Sunset windows replace `window` with:

```json
{"kind": "sunset", "start_offset_minutes": -30, "end_offset_minutes": 120}
```

Start offset is -180 to 180 minutes. End offset is -179 to 720 minutes, greater than start; total duration must be at most 720 minutes. Missing/unavailable `sun.sun` or an absent astronomical sunset makes the window ineligible. Calculation uses the hub location and home date through Home Assistant's [sun helper](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/sun.py), not the tablet clock.

Time policies are explicit: the hub's configured timezone owns the schedule; a nonexistent DST boundary skips that day's window; a repeated boundary starts at its first occurrence and ends at its last occurrence. Both repeated hours share one occurrence. Rule or scene edits change the occurrence identity, preventing a stale card from executing changed content.

V1 suppression policy is fixed: Dismiss lasts for the occurrence, Snooze lasts 30 minutes or until the occurrence ends, and a successful Run suppresses the occurrence for all users. There is no configurable cooldown or habitual learning.

## Recommendation and preview responses

GET returns `version`, `status`, nullable `suggestion`, and `refresh_at` when evaluation is available. Status is `not_configured` when no rules exist, `no_match` when none are eligible for this caller, `available` for a recommendation, or `unavailable` when evaluation cannot safely read its state. Disabled rules still count as configured.

A suggestion contains `rule_id`, `scene_id`, `scene` (`scene_id`, `name`, `icon`), `occurrence_id`, `generated_at`, `expires_at`, and a typed `reason`. Timestamps are UTC ISO 8601 strings. Occurrence IDs are opaque 64-character hashes; do not construct them on the client.

Reason codes are `time_window` and `time_and_state`. Parameters are `window_kind`, `match` and `condition_count`. The count is the number of configured conditions, not the number of active devices. Localize a truthful time/condition explanation; do not claim general household activity. All scene and condition references must be visible to the caller, even unmatched conditions in an Any rule.

Only entirely understood absolute targets are compared for already-satisfied suppression: plain on/off for lights/switches/fans, lock/unlock, and cover `set_position`. Complex action data and arbitrary actions are not guessed to be satisfied. Confirmed execution receipts cover those cases instead.

POST `/preview` accepts `{rule: ...}` and returns `version`, `eligible`, `reason` and nullable `suggestion`. Reasons include `eligible`, `disabled`, `scene_missing`, `not_visible`, `outside_window`, `conditions_not_met`, `scene_unavailable` and `already_satisfied`. Unknown references are rejected before evaluation. Preview does not apply a user's existing dismissal or execution receipt; it evaluates the proposed rule itself. It never executes or saves it.

## Actions and recovery

POST `/actions` accepts exactly `{action: "dismiss" | "snooze" | "run", occurrence_id: "..."}`. Dismiss/Snooze returns `status` (`dismissed` or `snoozed`) and `until`. Suppression is tied to the authenticated member, shared across that member's devices, not sent as a client-controlled user ID. Repeating a still-active Snooze does not extend it.

Run rechecks time, rule/scene content, conditions, room visibility, current control authorization and energy restrictions. It runs the scene through the hub's one scene executor, `async_execute_registry_scene`; there is no second device-command path. A persisted claim is acquired before dispatch. Simultaneous clients and repeated requests for the same occurrence cannot dispatch twice.

An execution receipt contains `occurrence_id`, `status`, `ok`, `expires_at`, `started_at` and, after a result, `succeeded_count`/`failed_count`. It does not include entity names or per-device errors. Status is:

- `executing`: HTTP 202; a command owner is still running. Repeating the same request reads the receipt rather than starting another run.
- `succeeded`: HTTP 200; all dispatched service calls succeeded. The occurrence is suppressed globally. This is a service result, not a claim that a physical motor has finished moving.
- `partial_failure`: HTTP 200 with `ok: false`. Show the failure/counts. If still eligible, the card carries `last_execution`. Repeating Run returns the same receipt and does not retry successful or uncertain device actions.
- `unknown`: HTTP 200 with `ok: false`. A dispatched operation was interrupted or was in flight at restart. Never automatically retry it; the user must inspect devices and choose any further manual action deliberately.

A claim rejected before the scene executor is called can be released safely. Storage failure before acquiring a claim cannot execute a scene. Once dispatch may have begun, uncertainty is retained rather than treated as permission to retry. Request replay is only available while the unchanged occurrence is current and its references remain visible; otherwise HTTP 409 is returned. Activating a scene by hand remains a separate, explicit workflow.

Error objects use `error` codes. Validation returns 400, denied permissions/lockouts 403, stale/ineligible occurrences, revision conflicts and a scene skipped because Energy Saving is active (`scene_skipped_energy_saving`) 409, bounded-state capacity 429, and unavailable action/management storage or service 503. Recommendation reads can instead return HTTP 200 with `status: unavailable` and no suggestion. A full active receipt/suppression store refuses new work rather than evicting safety records.

## Refresh and compatibility

Authenticated, subscribed WebSocket clients receive only `{type: "suggestions_changed", version: 1}`. No rule, room, entity, user, reason or occurrence is broadcast. Refetch the authorized GET response on this signal, reconnect/resume, `refresh_at` and `expires_at`. Do not keep a stale/offline Run button enabled. Notifications are coalesced and interest subscriptions are replaced when rules change or are disabled/deleted. Time boundaries and snooze expiry also schedule refreshes; evaluation never scans every device on state ticks.

The NOW snapshot carries `contextual_suggestion` with the same response envelope. Its `suggested_routine` field is a nullable scene object: the scene an administrator featured by hand, shown only when no contextual rules exist. `suggested_routine_source` labels it `featured_manual`. Contextual cards use the action endpoint above, not the ordinary scene activation endpoint. A featured scene is never converted to a time rule, and nothing is enabled automatically on upgrade.

## Storage and limits

State lives in the `suggestions_v1` namespace of the hub's SQLite key-value store and needs no schema migration. The document holds version, collection revision, rules, per-member suppressions and execution receipts. Each revision change and claim is one storage transaction. A hub rolled back to a version without suggestions ignores the namespace, and a later upgrade finds it intact. Factory reset clears it.

Limits are 64 rules, 16 conditions per rule, 4,096 suppression records and 2,048 execution receipts. Mutations prune records more than one day past occurrence expiry; active safety records are never evicted to make space. Startup converts interrupted execution claims to `unknown` without executing anything.
