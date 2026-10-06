"""Deterministic policy and real-SQLite contracts; no live home commands."""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parents[1] / "custom_components/casasmart"
package = ModuleType("phase4_fixture")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
policy = importlib.import_module("phase4_fixture.suggestions")
storage = importlib.import_module("phase4_fixture.storage")
stores = importlib.import_module("phase4_fixture.suggestion_store")


def rule(**changes):
    return policy.validate_rule(
        {
            "rule_id": "night",
            "scene_id": "scene-night",
            "enabled": True,
            "priority": 0,
            "weekdays": list(range(7)),
            "window": {"kind": "fixed", "start": "23:00", "end": "06:00"},
            "conditions": [{"entity_id": "light.one", "state": "on"}],
            "match": "all",
            **changes,
        }
    )


def scene(**changes):
    return {
        "scene_id": "scene-night",
        "name": "Good night",
        "icon": "moon",
        "entities": [{"entity_id": "light.one", "action": "turn_off", "data": {}}],
        **changes,
    }


class PolicyTest(unittest.TestCase):
    def evaluate(self, rules=None, **kwargs):
        args = {
            "rule": rules or rule(),
            "scene": scene(),
            "states": {"light.one": {"state": "on"}},
            "now": datetime(2026, 10, 5, 23, 30, tzinfo=UTC),
            "zone": ZoneInfo("UTC"),
            "sunset": lambda day: None,
            "visible": lambda eid: True,
        }
        args.update(kwargs)
        return policy.evaluate(**args)

    def test_fixed_boundaries_and_overnight_weekday_owner(self):
        monday = rule(weekdays=[0])
        for day, hour, minute, expected in [
            (5, 22, 59, False),
            (5, 23, 0, True),
            (6, 0, 0, True),
            (6, 5, 59, True),
            (6, 6, 0, False),
            (6, 23, 0, False),
        ]:
            with self.subTest(day=day, hour=hour):
                result, _ = self.evaluate(
                    monday, now=datetime(2026, 10, day, hour, minute, tzinfo=UTC)
                )
                self.assertEqual(result is not None, expected)

    def test_timezone_is_home_timezone_and_occurrence_stable(self):
        zone = ZoneInfo("Asia/Riyadh")
        first, _ = self.evaluate(
            zone=zone, now=datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
        )
        second, _ = self.evaluate(
            zone=zone, now=datetime(2026, 10, 5, 23, 0, tzinfo=UTC)
        )
        self.assertEqual(first["occurrence_id"], second["occurrence_id"])
        self.assertEqual(first["expires_at"], "2026-10-06T03:00:00+00:00")

    def test_dst_gap_skips_nonexistent_boundary(self):
        configured = rule(window={"kind": "fixed", "start": "02:30", "end": "04:00"})
        result, _ = self.evaluate(
            configured,
            zone=ZoneInfo("America/New_York"),
            now=datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        )
        self.assertIsNone(result)

    def test_dst_fold_is_one_occurrence_across_both_repeated_hours(self):
        configured = rule(window={"kind": "fixed", "start": "01:00", "end": "02:00"})
        results = [
            self.evaluate(
                configured,
                zone=ZoneInfo("America/New_York"),
                now=datetime(2026, 11, 1, h, 30, tzinfo=UTC),
            )[0]
            for h in (5, 6)
        ]
        self.assertEqual(results[0]["occurrence_id"], results[1]["occurrence_id"])
        self.assertEqual(results[0]["expires_at"], "2026-11-01T07:00:00+00:00")

    def test_sunset_offsets_and_missing_sun(self):
        configured = rule(
            window={
                "kind": "sunset",
                "start_offset_minutes": -30,
                "end_offset_minutes": 120,
            }
        )

        def sunset(day):
            return datetime(day.year, day.month, day.day, 18, tzinfo=UTC)

        result, _ = self.evaluate(
            configured, sunset=sunset, now=datetime(2026, 10, 5, 17, 30, tzinfo=UTC)
        )
        self.assertEqual(result["expires_at"], "2026-10-05T20:00:00+00:00")
        self.assertIsNone(
            self.evaluate(configured, now=datetime(2026, 10, 5, 18, tzinfo=UTC))[0]
        )

    def test_all_any_and_unknown_values(self):
        conditions = [
            {"entity_id": "light.one", "state": "on"},
            {"entity_id": "switch.two", "state": "on"},
        ]
        for unknown in ("unknown", "unavailable", None):
            states = {"light.one": {"state": "on"}, "switch.two": {"state": unknown}}
            self.assertIsNone(
                self.evaluate(rule(conditions=conditions), states=states)[0]
            )
            self.assertIsNotNone(
                self.evaluate(rule(conditions=conditions, match="any"), states=states)[
                    0
                ]
            )
        self.assertEqual(
            self.evaluate(states={"light.one": {"state": "unknown"}})[1],
            "conditions_not_met",
        )

    def test_permission_checks_all_references_even_any_unmatched_condition(self):
        configured = rule(
            match="any",
            conditions=[
                {"entity_id": "light.one", "state": "on"},
                {"entity_id": "switch.private", "state": "on"},
            ],
        )
        self.assertEqual(
            self.evaluate(configured, visible=lambda eid: eid != "switch.private")[1],
            "not_visible",
        )
        self.assertEqual(self.evaluate(scene=None)[1], "scene_missing")

    def test_absolute_targets_only_and_no_execution_side_effects(self):
        states = {"light.one": {"state": "off"}}
        self.assertEqual(
            self.evaluate(rule(conditions=[]), states=states)[1], "already_satisfied"
        )
        self.assertIsNotNone(
            self.evaluate(
                rule(conditions=[]),
                states=states,
                scene=scene(entities=[{"entity_id": "light.one", "action": "toggle"}]),
            )[0]
        )
        self.assertIsNotNone(
            self.evaluate(
                rule(conditions=[]),
                states={"light.one": {"state": "on"}},
                scene=scene(
                    entities=[
                        {
                            "entity_id": "light.one",
                            "action": "turn_on",
                            "data": {"brightness": 80},
                        }
                    ]
                ),
            )[0]
        )

    def test_scene_and_rule_edits_change_occurrence_identity(self):
        a = self.evaluate()[0]["occurrence_id"]
        self.assertNotEqual(a, self.evaluate(rule(priority=1))[0]["occurrence_id"])
        self.assertNotEqual(
            a, self.evaluate(scene=scene(name="Changed"))[0]["occurrence_id"]
        )

    def test_validation_fail_closed_and_disabled_default(self):
        raw = rule()
        raw.pop("enabled")
        self.assertFalse(policy.validate_rule(raw)["enabled"])
        for changed in [
            {"priority": True},
            {"enabled": "true"},
            {"weekdays": [0, 0]},
            {"weekdays": []},
            {"conditions": [{"entity_id": "script.any", "state": "on"}]},
            {"window": {"kind": "fixed", "start": "25:00", "end": "06:00"}},
            {"window": {"kind": "fixed", "start": "06:00", "end": "06:00"}},
            {
                "window": {
                    "kind": "sunset",
                    "start_offset_minutes": 30,
                    "end_offset_minutes": 10,
                }
            },
            {"command": "turn_on"},
        ]:
            with (
                self.subTest(changed=changed),
                self.assertRaises(policy.SuggestionError),
            ):
                policy.validate_rule({**rule(), **changed})


class StoreTest(unittest.TestCase):
    def test_capacity_rejects_without_evicting_active_claims(self):
        record = self.store.snapshot()
        receipt = {"status": "unknown", "expires_at": self.suggestion["expires_at"]}
        record["executions"] = {str(i): receipt for i in range(2048)}
        self.store.table["state"] = record
        with self.assertRaises(policy.SuggestionError) as failure:
            self.store.claim(self.suggestion, self.now)
        self.assertEqual(failure.exception.status, 429)
        self.assertEqual(len(self.store.snapshot()["executions"]), 2048)

    def test_persist_failure_rolls_back_claim_before_any_dispatch(self):
        from unittest.mock import patch

        table_type = type(self.store.table)
        original = table_type.__setitem__

        def fail(table, key, value):
            original(table, key, value)
            raise RuntimeError("injected disk failure")

        with (
            patch.object(table_type, "__setitem__", fail),
            self.assertRaises(RuntimeError),
        ):
            self.store.claim(self.suggestion, self.now)
        self.assertEqual(self.store.snapshot()["executions"], {})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = storage.HubStorage(Path(self.tmp.name) / "hub.db")
        self.db.open()
        self.addCleanup(self.db.close)
        self.store = stores.SuggestionStore(self.db)
        self.now = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
        self.suggestion = policy.evaluate(
            rule(),
            scene(),
            {"light.one": {"state": "on"}},
            self.now,
            ZoneInfo("UTC"),
            lambda day: None,
            lambda eid: True,
        )[0]

    def test_initial_upgrade_and_rollback_do_not_enable_or_modify_old_data(self):
        self.db.table("now_config")["global"] = {"suggested_scene_id": "legacy"}
        version = self.db.schema_version
        self.assertEqual(self.store.snapshot()["rules"], [])
        self.store.replace_rules(0, [rule(enabled=False)])
        self.assertEqual(self.db.schema_version, version)
        self.assertEqual(
            self.db.table("now_config")["global"], {"suggested_scene_id": "legacy"}
        )
        self.db.close()
        self.db.open()
        self.assertFalse(
            stores.SuggestionStore(self.db).snapshot()["rules"][0]["enabled"]
        )

    def test_revision_conflict_and_sqlite_rollback(self):
        self.store.replace_rules(0, [rule()])
        with self.assertRaises(policy.SuggestionError):
            self.store.replace_rules(0, [])
        with self.assertRaises(RuntimeError), self.db.transaction():
            self.store.replace_rules(1, [])
            raise RuntimeError("disk transaction fails")
        self.assertEqual(self.store.snapshot()["revision"], 1)
        self.assertEqual(len(self.store.snapshot()["rules"]), 1)

    def test_concurrent_claims_execute_at_most_once(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(
                    lambda _: self.store.claim(self.suggestion, self.now), range(2)
                )
            )
        self.assertEqual(sum(claimed for claimed, _ in results), 1)

    def test_suppression_is_per_user_and_survives_restart(self):
        self.store.suppress("one", self.suggestion, "dismiss", self.now)
        self.store.suppress("two", self.suggestion, "snooze", self.now)
        self.db.close()
        self.db.open()
        data = stores.SuggestionStore(self.db).snapshot()
        first = data["suppressions"][
            self.store.suppression_key("one", self.suggestion["occurrence_id"])
        ]
        second = data["suppressions"][
            self.store.suppression_key("two", self.suggestion["occurrence_id"])
        ]
        self.assertEqual(first["until"], self.suggestion["expires_at"])
        self.assertEqual(
            second["until"], (self.now + timedelta(minutes=30)).isoformat()
        )

    def test_restart_retains_unknown_claim_without_reexecution(self):
        self.store.claim(self.suggestion, self.now)
        self.db.close()
        self.db.open()
        self.store.recover()
        claimed, receipt = self.store.claim(self.suggestion, self.now)
        self.assertFalse(claimed)
        self.assertEqual(receipt["status"], "unknown")

    def test_partial_result_is_not_success_or_retryable_claim(self):
        self.store.claim(self.suggestion, self.now)
        receipt = self.store.finish(
            self.suggestion["occurrence_id"],
            {"ok": False, "results": [{"ok": True}, {"ok": False}]},
        )
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertFalse(receipt["ok"])
        self.assertFalse(self.store.claim(self.suggestion, self.now)[0])
        self.assertEqual(receipt["failed_count"], 1)

    def test_rule_limit_and_duplicate_ids(self):
        with self.assertRaises(policy.SuggestionError):
            self.store.replace_rules(0, [rule(), rule()])
        with self.assertRaises(policy.SuggestionError):
            self.store.replace_rules(0, [rule(rule_id=f"r{i}") for i in range(65)])
