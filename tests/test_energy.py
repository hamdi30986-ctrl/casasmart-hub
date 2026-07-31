"""P1 tests for the persistent, Home-Assistant-free Energy Saving engine."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from energy import (  # noqa: E402
    CONFIG_SCHEMA_VERSION,
    EVENT_ACTIVATED,
    EVENT_CONFIG_RESET,
    EVENT_CONFIG_UPDATED,
    EVENT_DEACTIVATED,
    EVENT_OCCUPANCY_CHANGED,
    EVENT_REAPPLIED,
    EVENT_RELEASED,
    EVENT_RELEASES_CLEARED,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_SMART,
    EnergyAlreadyActiveError,
    EnergyConfigError,
    EnergyEngine,
    EnergyError,
    EnergyInactiveError,
    EnergySetupRequiredError,
    UnknownEnergyLevelError,
    default_level_config,
    validate_level_config,
)
from storage import HubStorage  # noqa: E402


class _Clock:
    def __init__(self, start: float = 1000) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _complete_config(level: str) -> dict:
    config = default_level_config(level)
    config.update(
        {
            "excluded_rooms": ["kitchen"],
            "gang_keepers": {
                "gang.living": (
                    ["switch.living_left", "switch.living_center"]
                    if level == LEVEL_LOW
                    else ["switch.living_left"]
                )
            },
            "light_keepers": {
                "living": ["light.living_main", "light.living_lamp"]
            },
            "plug_offs": ["switch.iron"],
            "heaters": [
                {"entity_id": "switch.heater_1", "turn_off": True},
                {"entity_id": "switch.heater_2", "turn_off": False},
            ],
            "ac_keepers": (
                {}
                if level == LEVEL_LOW
                else {
                    "living": (
                        ["climate.living_main"]
                        if level == LEVEL_MEDIUM
                        else []
                    )
                }
            ),
            "setup_complete": True,
        }
    )
    return config


class EnergyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "hub.db"
        self.storage = HubStorage(self.db_path)
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.clock = _Clock()
        self.engine = self._make_engine()

    def _make_engine(self) -> EnergyEngine:
        engine = EnergyEngine(
            self.storage.table("energy_configs"),
            self.storage.table("energy_state"),
            self.storage.energy_events(),
            clock=self.clock,
        )
        engine.warm_up()
        return engine

    def _configure(self, level: str) -> dict:
        return self.engine.replace_config(level, _complete_config(level))


class EnergyConfigTests(EnergyTestCase):
    def test_defaults_are_level_specific_and_fresh(self):
        low = default_level_config(LEVEL_LOW)
        smart = default_level_config(LEVEL_SMART)
        self.assertEqual(low["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertNotIn("lockout_enabled", low)
        self.assertTrue(smart["lockout_enabled"])
        low["excluded_rooms"].append("x")
        self.assertEqual(default_level_config(LEVEL_LOW)["excluded_rooms"], [])

    def test_unknown_level_is_rejected(self):
        with self.assertRaises(UnknownEnergyLevelError):
            default_level_config("high")
        with self.assertRaises(UnknownEnergyLevelError):
            self.engine.get_config("hotel")

    def test_low_gang_groups_require_exactly_two_keepers(self):
        config = default_level_config(LEVEL_LOW)
        for picks in ([], ["switch.a"], ["switch.a", "switch.b", "switch.c"]):
            config["gang_keepers"] = {"gang.x": picks}
            with self.assertRaises(EnergyConfigError, msg=repr(picks)):
                validate_level_config(LEVEL_LOW, config)
        config["gang_keepers"] = {"gang.x": ["switch.a", "switch.b"]}
        self.assertEqual(
            validate_level_config(LEVEL_LOW, config)["gang_keepers"]["gang.x"],
            ["switch.a", "switch.b"],
        )

    def test_medium_and_smart_gangs_require_one_keeper(self):
        for level in (LEVEL_MEDIUM, LEVEL_SMART):
            config = default_level_config(level)
            config["gang_keepers"] = {"gang.x": ["switch.a", "switch.b"]}
            with self.assertRaises(EnergyConfigError, msg=level):
                validate_level_config(level, config)
            config["gang_keepers"] = {"gang.x": ["switch.a"]}
            self.assertEqual(
                validate_level_config(level, config)["gang_keepers"]["gang.x"],
                ["switch.a"],
            )

    def test_light_picks_must_be_nonempty_unique_entities(self):
        config = default_level_config(LEVEL_LOW)
        for picks in (
            [],
            ["light.a", "light.a"],
            ["not-an-entity"],
        ):
            config["light_keepers"] = {"living": picks}
            with self.assertRaises(EnergyConfigError, msg=repr(picks)):
                validate_level_config(LEVEL_LOW, config)

    def test_ac_pick_rules_are_level_specific(self):
        low = default_level_config(LEVEL_LOW)
        low["ac_keepers"] = {"living": ["climate.a"]}
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_LOW, low)

        medium = default_level_config(LEVEL_MEDIUM)
        medium["ac_keepers"] = {"living": []}
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_MEDIUM, medium)
        medium["ac_keepers"] = {"living": ["climate.a"]}
        self.assertEqual(
            validate_level_config(LEVEL_MEDIUM, medium)["ac_keepers"]["living"],
            ["climate.a"],
        )

        smart = default_level_config(LEVEL_SMART)
        smart["ac_keepers"] = {"living": []}
        self.assertEqual(
            validate_level_config(LEVEL_SMART, smart)["ac_keepers"]["living"],
            [],
        )

    def test_heaters_are_explicit_off_or_keep_records(self):
        config = default_level_config(LEVEL_LOW)
        config["heaters"] = [
            {"entity_id": "switch.water_heater", "turn_off": True},
            {"entity_id": "switch.guest_heater", "turn_off": False},
        ]
        self.assertEqual(
            validate_level_config(LEVEL_LOW, config)["heaters"],
            config["heaters"],
        )
        invalid = (
            [{"entity_id": "switch.h"}],
            [{"entity_id": "switch.h", "turn_off": 1}],
            [
                {"entity_id": "switch.h", "turn_off": True},
                {"entity_id": "switch.h", "turn_off": False},
            ],
            [{"entity_id": "switch.h", "turn_off": True, "timer": 5}],
        )
        for heaters in invalid:
            config["heaters"] = heaters
            with self.assertRaises(EnergyConfigError, msg=repr(heaters)):
                validate_level_config(LEVEL_LOW, config)

    def test_duplicate_room_and_plug_picks_are_rejected(self):
        config = default_level_config(LEVEL_LOW)
        config["excluded_rooms"] = ["kitchen", "kitchen"]
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_LOW, config)
        config["excluded_rooms"] = []
        config["plug_offs"] = ["switch.iron", "switch.iron"]
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_LOW, config)

    def test_excluded_room_cannot_carry_room_picks(self):
        config = default_level_config(LEVEL_MEDIUM)
        config["excluded_rooms"] = ["living"]
        config["light_keepers"] = {"living": ["light.main"]}
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_MEDIUM, config)

    def test_unknown_fields_bad_schema_and_non_boolean_flags_fail_closed(self):
        smart = default_level_config(LEVEL_SMART)
        smart["schedule"] = {"at": "22:00"}
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_SMART, smart)
        smart.pop("schedule")
        smart["schema_version"] = 99
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_SMART, smart)
        smart["schema_version"] = CONFIG_SCHEMA_VERSION
        smart["lockout_enabled"] = 1
        with self.assertRaises(EnergyConfigError):
            validate_level_config(LEVEL_SMART, smart)

    def test_patch_is_resumable_persistent_and_audited(self):
        patched = self.engine.patch_config(
            LEVEL_LOW,
            {
                "excluded_rooms": ["kitchen", "baby-room"],
                "heaters": [
                    {"entity_id": "switch.heater", "turn_off": True}
                ],
            },
            actor="admin-1",
        )
        self.assertEqual(patched["excluded_rooms"], ["kitchen", "baby-room"])
        self.assertFalse(patched["setup_complete"])
        stored = self.storage.table("energy_configs")[LEVEL_LOW]
        self.assertEqual(stored, patched)
        event = self.engine.recent_events()[0]
        self.assertEqual(event["kind"], EVENT_CONFIG_UPDATED)
        self.assertEqual(
            event["data"]["changed_fields"], ["excluded_rooms", "heaters"]
        )
        self.assertEqual(event["data"]["actor"], "admin-1")

    def test_config_results_are_defensive_copies(self):
        saved = self._configure(LEVEL_LOW)
        saved["excluded_rooms"].append("tamper")
        fetched = self.engine.get_config(LEVEL_LOW)
        fetched["plug_offs"].clear()
        self.assertEqual(self.engine.get_config(LEVEL_LOW), _complete_config(LEVEL_LOW))

    def test_reset_restores_defaults_and_logs(self):
        self._configure(LEVEL_SMART)
        reset = self.engine.reset_config(LEVEL_SMART)
        self.assertEqual(reset, default_level_config(LEVEL_SMART))
        self.assertEqual(self.engine.recent_events()[0]["kind"], EVENT_CONFIG_RESET)

    def test_config_survives_reopen(self):
        expected = self._configure(LEVEL_MEDIUM)
        self.storage.close()
        self.storage.open()
        reopened = self._make_engine()
        self.assertEqual(reopened.get_config(LEVEL_MEDIUM), expected)

    def test_invalid_clock_cannot_partially_persist_a_config_write(self):
        before = self.engine.get_config(LEVEL_LOW)
        self.clock.t = float("nan")
        with self.assertRaises(EnergyError):
            self.engine.patch_config(
                LEVEL_LOW, {"excluded_rooms": ["kitchen"]}
            )
        self.assertEqual(self.engine.get_config(LEVEL_LOW), before)
        self.assertNotIn(LEVEL_LOW, self.storage.table("energy_configs"))

    def test_malformed_persisted_config_falls_back_to_incomplete_default(self):
        self.storage.table("energy_configs")[LEVEL_SMART] = {
            "setup_complete": True,
            "lockout_enabled": "yes",
        }
        engine = self._make_engine()
        self.assertEqual(
            engine.get_config(LEVEL_SMART), default_level_config(LEVEL_SMART)
        )


class EnergyStateTests(EnergyTestCase):
    def test_activation_requires_completed_setup(self):
        with self.assertRaises(EnergySetupRequiredError) as raised:
            self.engine.activate(LEVEL_LOW)
        self.assertEqual(raised.exception.level, LEVEL_LOW)
        self.assertFalse(self.engine.snapshot()["active"])

    def test_low_and_medium_lockout_is_inherent(self):
        for level in (LEVEL_LOW, LEVEL_MEDIUM):
            engine = self._make_engine()
            engine.replace_config(level, _complete_config(level))
            state = engine.activate(level)
            self.assertTrue(state["lockout_enabled"])
            engine.deactivate()

    def test_smart_activation_toggle_is_persisted_in_its_blob(self):
        self._configure(LEVEL_SMART)
        state = self.engine.activate(
            LEVEL_SMART,
            smart_lockout_enabled=False,
            actor="admin",
        )
        self.assertFalse(state["lockout_enabled"])
        self.assertFalse(
            self.engine.get_config(LEVEL_SMART)["lockout_enabled"]
        )
        event = self.engine.recent_events()[0]
        self.assertEqual(event["kind"], EVENT_ACTIVATED)
        self.assertFalse(event["data"]["lockout_enabled"])

    def test_non_smart_activation_rejects_smart_toggle(self):
        self._configure(LEVEL_LOW)
        with self.assertRaises(EnergyConfigError):
            self.engine.activate(LEVEL_LOW, smart_lockout_enabled=False)

    def test_second_activation_requires_explicit_deactivate(self):
        self._configure(LEVEL_LOW)
        self._configure(LEVEL_MEDIUM)
        self.engine.activate(LEVEL_LOW)
        with self.assertRaises(EnergyAlreadyActiveError):
            self.engine.activate(LEVEL_MEDIUM)
        self.assertEqual(self.engine.active_level, LEVEL_LOW)

    def test_deactivation_unlocks_without_restore(self):
        self._configure(LEVEL_LOW)
        self.engine.activate(LEVEL_LOW)
        self.engine.mark_released("light.main", room_id="living")
        state = self.engine.deactivate(actor="admin")
        self.assertFalse(state["active"])
        self.assertFalse(state["lockout_enabled"])
        self.assertEqual(state["released_entities"], [])
        event = self.engine.recent_events()[0]
        self.assertEqual(event["kind"], EVENT_DEACTIVATED)
        self.assertFalse(event["data"]["restored_devices"])
        self.assertEqual(event["data"]["released_count"], 1)

    def test_deactivation_is_idempotent_when_already_inactive(self):
        count = self.engine.stats()["events_total"]
        self.engine.deactivate()
        self.assertEqual(self.engine.stats()["events_total"], count)

    def test_reapply_requires_active_level(self):
        with self.assertRaises(EnergyInactiveError):
            self.engine.reapply()

    def test_reapply_clears_releases_but_keeps_smart_occupancy(self):
        self._configure(LEVEL_SMART)
        self.engine.activate(LEVEL_SMART)
        self.engine.set_room_occupancy("living", True)
        self.engine.mark_released("light.main", room_id="living")
        state = self.engine.reapply(actor="admin")
        self.assertEqual(state["released_entities"], [])
        self.assertTrue(state["room_occupancy"]["living"]["occupied"])
        self.assertEqual(self.engine.recent_events()[0]["kind"], EVENT_REAPPLIED)

    def test_state_and_revision_survive_reopen(self):
        self._configure(LEVEL_SMART)
        self.engine.activate(LEVEL_SMART)
        self.engine.set_room_occupancy("living", True)
        self.engine.mark_released(
            "climate.living", room_id="living", source="remote"
        )
        before = self.engine.snapshot()

        self.storage.close()
        self.storage.open()
        reopened = self._make_engine()

        self.assertEqual(reopened.snapshot(), before)
        self.assertTrue(reopened.is_released("climate.living"))

    def test_invalid_actor_cannot_partially_activate_or_release(self):
        self._configure(LEVEL_LOW)
        before = self.engine.snapshot()
        with self.assertRaises(EnergyConfigError):
            self.engine.activate(LEVEL_LOW, actor="   ")
        self.assertEqual(self.engine.snapshot(), before)

        self.engine.activate(LEVEL_LOW)
        with self.assertRaises(EnergyConfigError):
            self.engine.mark_released("light.main", actor="")
        self.assertEqual(self.engine.snapshot()["released_entities"], [])

    def test_corrupt_state_fails_inactive_instead_of_crashing(self):
        self.storage.table("energy_state")["current"] = {
            "active_level": "high",
            "lockout_enabled": "yes",
            "released_entities": ["bad"],
            "revision": -1,
        }
        engine = self._make_engine()
        self.assertEqual(engine.snapshot(), {
            "active_level": None,
            "activated_at": None,
            "last_applied_at": None,
            "lockout_enabled": False,
            "released_entities": [],
            "release_details": {},
            "room_occupancy": {},
            "revision": 0,
            "active": False,
            "release_count": 0,
        })

    def test_reboot_drops_impossible_occupancy_from_static_level(self):
        self.storage.table("energy_state")["current"] = {
            "active_level": LEVEL_LOW,
            "activated_at": 100,
            "last_applied_at": 100,
            "lockout_enabled": False,
            "released_entities": [],
            "release_details": {},
            "room_occupancy": {
                "living": {
                    "occupied": True,
                    "sensors_available": True,
                    "changed_at": 100,
                }
            },
            "revision": 2,
        }
        engine = self._make_engine()
        state = engine.snapshot()
        self.assertTrue(state["lockout_enabled"])
        self.assertEqual(state["room_occupancy"], {})

    def test_snapshot_is_a_defensive_copy(self):
        self._configure(LEVEL_LOW)
        self.engine.activate(LEVEL_LOW)
        snapshot = self.engine.snapshot()
        snapshot["released_entities"].append("light.fake")
        self.assertEqual(self.engine.snapshot()["released_entities"], [])


class EnergyReleaseAndOccupancyTests(EnergyTestCase):
    def setUp(self):
        super().setUp()
        self._configure(LEVEL_SMART)
        self.engine.activate(LEVEL_SMART)

    def test_release_set_is_idempotent_and_keeps_metadata(self):
        self.assertTrue(
            self.engine.mark_released(
                "light.living",
                room_id="living",
                source="wall_press",
                actor="admin",
            )
        )
        self.assertFalse(
            self.engine.mark_released(
                "light.living", room_id="living", source="duplicate"
            )
        )
        state = self.engine.snapshot()
        self.assertEqual(state["released_entities"], ["light.living"])
        self.assertEqual(
            state["release_details"]["light.living"],
            {
                "room_id": "living",
                "released_at": 1000,
                "source": "wall_press",
            },
        )
        released_events = self.engine.recent_events(kinds=[EVENT_RELEASED])
        self.assertEqual(len(released_events), 1)
        self.assertEqual(released_events[0]["data"]["actor"], "admin")

    def test_inactive_release_edge_is_ignored(self):
        self.engine.deactivate()
        before = self.engine.stats()["events_total"]
        self.assertFalse(self.engine.mark_released("light.living"))
        self.assertEqual(self.engine.stats()["events_total"], before)

    def test_room_empty_clear_only_removes_that_rooms_releases(self):
        self.engine.mark_released("light.living", room_id="living")
        self.engine.mark_released("climate.bedroom", room_id="bedroom")
        cleared = self.engine.clear_room_releases("living")
        self.assertEqual(cleared, ["light.living"])
        self.assertEqual(
            self.engine.snapshot()["released_entities"], ["climate.bedroom"]
        )
        event = self.engine.recent_events()[0]
        self.assertEqual(event["kind"], EVENT_RELEASES_CLEARED)
        self.assertEqual(event["room_id"], "living")

    def test_room_release_clear_is_smart_only(self):
        self.engine.deactivate()
        self._configure(LEVEL_LOW)
        self.engine.activate(LEVEL_LOW)
        self.engine.mark_released("light.living", room_id="living")
        self.assertEqual(self.engine.clear_room_releases("living"), [])
        self.assertTrue(self.engine.is_released("light.living"))

    def test_occupancy_edges_are_persisted_and_idempotent(self):
        self.assertTrue(self.engine.set_room_occupancy("living", True))
        revision = self.engine.snapshot()["revision"]
        self.assertFalse(self.engine.set_room_occupancy("living", True))
        self.assertEqual(self.engine.snapshot()["revision"], revision)
        self.clock.advance(45)
        self.assertTrue(self.engine.set_room_occupancy("living", False))
        record = self.engine.snapshot()["room_occupancy"]["living"]
        self.assertFalse(record["occupied"])
        self.assertEqual(record["changed_at"], 1045)
        events = self.engine.recent_events(kinds=[EVENT_OCCUPANCY_CHANGED])
        self.assertEqual(len(events), 2)

    def test_unavailable_sensor_state_is_explicit(self):
        self.assertTrue(
            self.engine.set_room_occupancy(
                "guest", None, sensors_available=False
            )
        )
        self.assertEqual(
            self.engine.snapshot()["room_occupancy"]["guest"],
            {
                "occupied": None,
                "sensors_available": False,
                "changed_at": 1000,
            },
        )
        with self.assertRaises(EnergyConfigError):
            self.engine.set_room_occupancy(
                "guest", True, sensors_available=False
            )
        with self.assertRaises(EnergyConfigError):
            self.engine.set_room_occupancy("guest", None)

    def test_occupancy_is_ignored_outside_smart(self):
        self.engine.deactivate()
        self._configure(LEVEL_MEDIUM)
        self.engine.activate(LEVEL_MEDIUM)
        self.assertFalse(self.engine.set_room_occupancy("living", True))
        self.assertEqual(self.engine.snapshot()["room_occupancy"], {})


class EnergyEventsAndStatsTests(EnergyTestCase):
    def test_event_store_orders_filters_summarizes_and_prunes(self):
        events = self.storage.energy_events()
        events.append(t=10, kind="a", level=LEVEL_LOW, data={"n": 1})
        events.append(t=20, kind="b", level=LEVEL_SMART, data={"n": 2})
        events.append(t=20, kind="a", data={"n": 3})
        self.assertEqual(
            [event["data"]["n"] for event in events.recent()], [3, 2, 1]
        )
        self.assertEqual(
            [event["kind"] for event in events.recent(since_t=20, kinds=["a"])],
            ["a"],
        )
        self.assertEqual(events.summary(), {
            "events_total": 3,
            "first_event_at": 10,
            "last_event_at": 20,
            "event_counts": {"a": 2, "b": 1},
        })
        self.assertEqual(events.prune(before_t=20), 1)
        self.assertEqual(events.summary()["events_total"], 2)
        events.clear()
        self.assertEqual(events.summary()["events_total"], 0)

    def test_event_store_rejects_bad_query_and_payload_shapes(self):
        events = self.storage.energy_events()
        with self.assertRaises(ValueError):
            events.recent(limit=0)
        with self.assertRaises(ValueError):
            events.recent(kinds=[])
        with self.assertRaises(TypeError):
            events.append(t=1, kind="x", data=[])
        with self.assertRaises(TypeError):
            events.append(t=1, kind="x", data={"nan": float("nan")})
        with self.assertRaises(ValueError):
            events.append(t=-1, kind="x")

    def test_public_record_event_is_the_p2_p3_audit_seam(self):
        event = self.engine.record_event(
            "automation_disabled",
            level=LEVEL_MEDIUM,
            entity_id="automation.away",
            data={"was_enabled": True},
        )
        self.assertEqual(event["kind"], "automation_disabled")
        self.assertEqual(event["entity_id"], "automation.away")
        with self.assertRaises(UnknownEnergyLevelError):
            self.engine.record_event("x", level="high")

    def test_stats_lite_reports_only_factual_operational_counts(self):
        self._configure(LEVEL_SMART)
        self.engine.activate(LEVEL_SMART)
        self.engine.mark_released("light.living", room_id="living")
        self.engine.set_room_occupancy("living", True)
        self.engine.set_room_occupancy(
            "guest", None, sensors_available=False
        )
        stats = self.engine.stats()
        self.assertTrue(stats["active"])
        self.assertEqual(stats["active_level"], LEVEL_SMART)
        self.assertEqual(stats["released_devices"], 1)
        self.assertEqual(stats["occupied_rooms"], 1)
        self.assertEqual(stats["empty_rooms"], 0)
        self.assertEqual(stats["rooms_with_sensor_issues"], 1)
        forbidden = {"cost", "money", "kwh", "saved_kwh", "co2"}
        self.assertTrue(forbidden.isdisjoint(stats))

    def test_stats_since_filter_applies_to_event_counts(self):
        self.engine.record_event("old")
        self.clock.advance(10)
        self.engine.record_event("new")
        stats = self.engine.stats(since_t=1010)
        self.assertEqual(stats["events_total"], 1)
        self.assertEqual(stats["event_counts"], {"new": 1})

    def test_warm_up_prunes_events_older_than_180_days(self):
        events = self.storage.energy_events()
        events.append(t=1, kind="old")
        self.clock.t = 181 * 24 * 3600
        self._make_engine()
        self.assertEqual(events.summary()["events_total"], 0)

    def test_invalid_clock_is_rejected(self):
        self.clock.t = float("nan")
        with self.assertRaises(EnergyError):
            self._make_engine()


if __name__ == "__main__":
    unittest.main()
