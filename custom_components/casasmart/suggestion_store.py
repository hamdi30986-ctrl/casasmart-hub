"""Additive single-document SQLite state with atomic revision/claim updates."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from .suggestions import MAX_RULES, SuggestionError, integer, validate_rule


class SuggestionStore:
    def __init__(self, storage):
        self.storage = storage
        self.table = storage.table("suggestions_v1")

    def snapshot(self):
        result = self.table.get("state") or {
            "version": 1,
            "revision": 0,
            "rules": [],
            "suppressions": {},
            "executions": {},
        }
        if result.get("version") != 1:
            raise SuggestionError("unsupported_suggestion_storage", 503)
        return deepcopy(result)

    def recover(self):
        # A process restart cannot tell whether a motor command was accepted.
        # Retain the claim as unknown; NEVER automatically rerun it.
        with self.storage.transaction():
            data = self.snapshot()
            changed = False
            for receipt in data["executions"].values():
                if receipt["status"] == "executing":
                    receipt["status"] = "unknown"
                    changed = True
            if changed:
                self.table["state"] = data

    def replace_rules(self, revision, raw_rules):
        if (
            not integer(revision, 0, 2**53)
            or not isinstance(raw_rules, list)
            or len(raw_rules) > MAX_RULES
        ):
            raise SuggestionError("invalid_rules")
        rules = [validate_rule(rule) for rule in raw_rules]
        if len({r["rule_id"] for r in rules}) != len(rules):
            raise SuggestionError("duplicate_rule_id")
        with self.storage.transaction():
            data = self.snapshot()
            if data["revision"] != revision:
                raise SuggestionError("revision_conflict", 409)
            data["rules"], data["revision"] = rules, revision + 1
            self.table["state"] = data
        return {"version": 1, "revision": revision + 1, "rules": rules}

    @staticmethod
    def suppression_key(member, occurrence):
        return f"{len(member)}:{member}:{occurrence}"

    @staticmethod
    def _prune(data, now):
        for field in ("executions", "suppressions"):
            data[field] = {
                key: value
                for key, value in data[field].items()
                if datetime.fromisoformat(value["expires_at"]) + timedelta(days=1) > now
            }

    def suppress(self, member, suggestion, action, now):
        with self.storage.transaction():
            data = self.snapshot()
            self._prune(data, now)
            key = self.suppression_key(
                member, suggestion.get("suppression_id", suggestion["occurrence_id"])
            )
            if key not in data["suppressions"] and len(data["suppressions"]) >= 4096:
                raise SuggestionError("suppression_capacity", 429)
            expires = datetime.fromisoformat(suggestion["expires_at"])
            until = (
                min(now + timedelta(minutes=30), expires)
                if action == "snooze"
                else expires
            )
            data["suppressions"][key] = {
                "until": until.isoformat(),
                "expires_at": expires.isoformat(),
                "action": action,
            }
            self.table["state"] = data
        return {
            "status": "snoozed" if action == "snooze" else "dismissed",
            "until": until.isoformat(),
        }

    def claim(self, suggestion, now):
        occurrence = suggestion["occurrence_id"]
        with self.storage.transaction():
            data = self.snapshot()
            self._prune(data, now)
            receipt = data["executions"].get(occurrence)
            if receipt is None and suggestion.get("suppression_id"):
                receipt = next(
                    (
                        r
                        for r in data["executions"].values()
                        if r.get("suppression_id") == suggestion["suppression_id"]
                    ),
                    None,
                )
            if receipt:
                return False, receipt
            if len(data["executions"]) >= 2048:
                raise SuggestionError("execution_capacity", 429)
            receipt = {
                "occurrence_id": occurrence,
                "status": "executing",
                "ok": False,
                "expires_at": suggestion["expires_at"],
                "started_at": now.isoformat(),
                "suppression_id": suggestion.get("suppression_id", occurrence),
            }
            data["executions"][occurrence] = receipt
            self.table["state"] = data
            return True, receipt

    def select_generated_rooms(self, scope_key, start, ranked_ids):
        """Freeze targets for two hours, including across a hub restart."""
        with self.storage.transaction():
            data = self.snapshot()
            selections = {
                k: v
                for k, v in data.get("generated_selections", {}).items()
                if v["start"] == start
            }
            if scope_key not in selections:
                if len(selections) >= 1024:
                    raise SuggestionError("selection_capacity", 429)
                selections[scope_key] = {"start": start, "room_ids": ranked_ids[:2]}
            elif len(selections[scope_key]["room_ids"]) < 2:
                # Startup may precede the first device state. Fill vacant slots
                # when activity arrives; never reshuffle already chosen rooms.
                chosen = selections[scope_key]["room_ids"]
                selections[scope_key] = {
                    "start": start,
                    "room_ids": (
                        chosen + [rid for rid in ranked_ids if rid not in chosen]
                    )[:2],
                }
            if selections != data.get("generated_selections"):
                data["generated_selections"] = selections
                self.table["state"] = data
            return selections[scope_key]["room_ids"]

    def finish(self, occurrence, result):
        with self.storage.transaction():
            data = self.snapshot()
            receipt = data["executions"][occurrence]
            if receipt["status"] != "executing":
                return receipt
            outcomes = result.get("results", [])
            receipt.update(
                status="succeeded" if result.get("ok") is True else "partial_failure",
                ok=result.get("ok") is True,
                succeeded_count=sum(i.get("ok") is True for i in outcomes),
                failed_count=sum(i.get("ok") is not True for i in outcomes),
            )
            self.table["state"] = data
            return receipt

    def unknown(self, occurrence):
        with self.storage.transaction():
            data = self.snapshot()
            data["executions"][occurrence]["status"] = "unknown"
            self.table["state"] = data

    def abort_before_dispatch(self, occurrence):
        """Release only a claim whose owner has not called the scene executor."""
        with self.storage.transaction():
            data = self.snapshot()
            if data["executions"].get(occurrence, {}).get("status") == "executing":
                del data["executions"][occurrence]
                self.table["state"] = data
