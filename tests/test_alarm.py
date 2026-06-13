"""Unit tests for B13: the hub-side alarm state machine.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage  # noqa: E402
from alarm import (  # noqa: E402
    DEFAULT_ENTRY_DELAY_SECONDS,
    DEFAULT_EXIT_DELAY_SECONDS,
    EVENT_ARMED,
    EVENT_DISARMED,
    EVENT_ENTRY_DELAY,
    EVENT_LIFE_SAFETY,
    EVENT_TAMPER,
    EVENT_TRIGGERED,
    MODE_AWAY,
    MODE_DISARMED,
    MODE_HOME,
    MODE_NIGHT,
    MODE_PENDING,
    MODE_TRIGGERED,
    ZONE_ENTRY,
    ZONE_INTERIOR,
    ZONE_LIFE_SAFETY,
    ZONE_PERIMETER,
    AlarmEngine,
    AlarmError,
    UnknownZoneError,
    active_zones_for_mode,
)


class _Clock:
    """Hand-cranked clock so delays are deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class AlarmTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.storage.close)
        self.clock = _Clock()
        self.alerts: list[dict] = []
        self.engine = self._make_engine()

    def _make_engine(self) -> AlarmEngine:
        engine = AlarmEngine(
            self.storage.table("alarm_state"),
            self.storage.table("alarm_zones"),
            self.storage.table("alarm_history"),
            self.storage.table("alarm_settings"),
            alert_sink=self.alerts.append,
            clock=self.clock,
        )
        engine.warm_up()
        return engine

    def _seed_zones(self):
        self.engine.set_zone("binary_sensor.front_door", ZONE_ENTRY, "Front Door")
        self.engine.set_zone("binary_sensor.window", ZONE_PERIMETER, "Living Window")
        self.engine.set_zone("binary_sensor.hall_motion", ZONE_INTERIOR, "Hall Motion")
        self.engine.set_zone("binary_sensor.smoke", ZONE_LIFE_SAFETY, "Kitchen Smoke")

    # -- zone config -----------------------------------------------------------

    def test_set_and_remove_zone(self):
        rec = self.engine.set_zone("binary_sensor.door", ZONE_ENTRY, "Door")
        self.assertEqual(rec["zone"], ZONE_ENTRY)
        self.assertEqual(self.engine.zone_of("binary_sensor.door"), ZONE_ENTRY)
        self.engine.remove_zone("binary_sensor.door")
        self.assertIsNone(self.engine.zone_of("binary_sensor.door"))

    def test_remove_unknown_zone_raises(self):
        with self.assertRaises(UnknownZoneError):
            self.engine.remove_zone("binary_sensor.ghost")

    def test_invalid_zone_rejected(self):
        with self.assertRaises(AlarmError):
            self.engine.set_zone("binary_sensor.x", "basement", "X")

    def test_blank_entity_rejected(self):
        with self.assertRaises(AlarmError):
            self.engine.set_zone("   ", ZONE_ENTRY, "X")

    def test_active_zones_per_mode(self):
        self.assertEqual(
            active_zones_for_mode(MODE_AWAY),
            frozenset({ZONE_PERIMETER, ZONE_INTERIOR, ZONE_ENTRY}),
        )
        self.assertEqual(active_zones_for_mode(MODE_HOME), frozenset({ZONE_PERIMETER}))
        self.assertEqual(
            active_zones_for_mode(MODE_NIGHT),
            frozenset({ZONE_PERIMETER, ZONE_ENTRY}),
        )
        self.assertEqual(active_zones_for_mode(MODE_DISARMED), frozenset())

    # -- arm / disarm ----------------------------------------------------------

    def test_arm_disarm_cycle(self):
        snap = self.engine.arm(MODE_AWAY)
        self.assertEqual(snap["mode"], MODE_AWAY)
        snap = self.engine.disarm()
        self.assertEqual(snap["mode"], MODE_DISARMED)
        kinds = [e["kind"] for e in self.engine.history()]
        self.assertEqual(kinds, [EVENT_DISARMED, EVENT_ARMED])  # newest first

    def test_cannot_arm_to_pending_or_triggered(self):
        with self.assertRaises(AlarmError):
            self.engine.arm(MODE_PENDING)
        with self.assertRaises(AlarmError):
            self.engine.arm(MODE_TRIGGERED)

    def test_arm_records_actor(self):
        self.engine.arm(MODE_AWAY, actor="user-admin")
        armed = self.engine.history()[0]
        self.assertEqual(armed["actor"], "user-admin")

    def test_invalid_delay_rejected(self):
        with self.assertRaises(AlarmError):
            self.engine.arm(MODE_AWAY, exit_delay=-5)
        with self.assertRaises(AlarmError):
            self.engine.arm(MODE_AWAY, entry_delay=99999)

    # -- trigger logic ---------------------------------------------------------

    def test_unmapped_sensor_ignored(self):
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.assertIsNone(self.engine.process_sensor("binary_sensor.ghost", True))

    def test_perimeter_triggers_immediately_in_away(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        event = self.engine.process_sensor("binary_sensor.window", True)
        self.assertEqual(event["kind"], EVENT_TRIGGERED)
        self.assertEqual(self.engine.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(len(self.alerts), 1)

    def test_motion_ignored_in_home(self):
        self._seed_zones()
        self.engine.arm(MODE_HOME, exit_delay=0)
        self.assertIsNone(self.engine.process_sensor("binary_sensor.hall_motion", True))
        self.assertEqual(self.engine.snapshot()["mode"], MODE_HOME)

    def test_motion_triggers_in_away(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        event = self.engine.process_sensor("binary_sensor.hall_motion", True)
        self.assertEqual(event["kind"], EVENT_TRIGGERED)

    def test_entry_ignored_in_home(self):
        # Home = perimeter only; entry zone is NOT active.
        self._seed_zones()
        self.engine.arm(MODE_HOME, exit_delay=0)
        self.assertIsNone(self.engine.process_sensor("binary_sensor.front_door", True))

    def test_entry_active_in_night(self):
        self._seed_zones()
        self.engine.arm(MODE_NIGHT, exit_delay=0)
        event = self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertEqual(event["kind"], EVENT_ENTRY_DELAY)
        self.assertEqual(self.engine.snapshot()["mode"], MODE_PENDING)

    def test_sensor_clear_never_triggers(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.assertIsNone(self.engine.process_sensor("binary_sensor.window", False))

    def test_exit_delay_grace(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=60)
        # Within grace window: opening a perimeter door does nothing.
        self.assertIsNone(self.engine.process_sensor("binary_sensor.window", True))
        self.clock.advance(61)
        event = self.engine.process_sensor("binary_sensor.window", True)
        self.assertEqual(event["kind"], EVENT_TRIGGERED)

    # -- entry delay countdown -------------------------------------------------

    def test_entry_delay_then_disarm_no_alarm(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertEqual(self.engine.snapshot()["mode"], MODE_PENDING)
        self.assertEqual(len(self.alerts), 0)  # no alert during countdown
        self.clock.advance(DEFAULT_ENTRY_DELAY_SECONDS - 1)
        self.engine.disarm()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_DISARMED)
        # tick after disarm must not resurrect the alarm
        self.assertIsNone(self.engine.tick())
        self.assertEqual(len(self.alerts), 0)

    def test_entry_delay_lapses_to_triggered(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertIsNone(self.engine.tick())  # deadline not reached yet
        self.clock.advance(DEFAULT_ENTRY_DELAY_SECONDS + 1)
        event = self.engine.tick()
        self.assertEqual(event["kind"], EVENT_TRIGGERED)
        self.assertEqual(self.engine.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(len(self.alerts), 1)

    def test_second_entry_sensor_during_pending_ignored(self):
        self._seed_zones()
        self.engine.set_zone("binary_sensor.back_door", ZONE_ENTRY, "Back Door")
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertIsNone(self.engine.process_sensor("binary_sensor.back_door", True))

    def test_pending_deadline_exposed(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0, entry_delay=45)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertEqual(self.engine.pending_deadline(), self.clock.t + 45)

    # -- life safety (always armed) --------------------------------------------

    def test_life_safety_fires_while_disarmed(self):
        self._seed_zones()
        self.assertEqual(self.engine.snapshot()["mode"], MODE_DISARMED)
        event = self.engine.process_sensor("binary_sensor.smoke", True)
        self.assertEqual(event["kind"], EVENT_LIFE_SAFETY)
        self.assertTrue(event["life_safety"])
        self.assertEqual(self.engine.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(len(self.alerts), 1)

    def test_life_safety_fires_immediately_when_armed(self):
        self._seed_zones()
        self.engine.arm(MODE_HOME, exit_delay=30)  # even inside exit grace
        event = self.engine.process_sensor("binary_sensor.smoke", True)
        self.assertEqual(event["kind"], EVENT_LIFE_SAFETY)

    def test_life_safety_clear_does_nothing(self):
        self._seed_zones()
        self.assertIsNone(self.engine.process_sensor("binary_sensor.smoke", False))

    # -- tamper ----------------------------------------------------------------

    def test_tamper_while_armed_alerts_without_triggering(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        event = self.engine.process_sensor_offline("binary_sensor.hall_motion")
        self.assertEqual(event["kind"], EVENT_TAMPER)
        # Tamper does NOT escalate to a full alarm.
        self.assertEqual(self.engine.snapshot()["mode"], MODE_AWAY)
        self.assertEqual(len(self.alerts), 1)

    def test_tamper_while_disarmed_ignored(self):
        self._seed_zones()
        self.assertIsNone(
            self.engine.process_sensor_offline("binary_sensor.hall_motion")
        )

    # -- persistence / reboot --------------------------------------------------

    def test_arm_state_survives_reboot(self):
        self._seed_zones()
        self.engine.arm(MODE_NIGHT, exit_delay=0, entry_delay=20)
        # Simulate a hub restart: fresh engine over the same storage.
        revived = self._make_engine()
        snap = revived.snapshot()
        self.assertEqual(snap["mode"], MODE_NIGHT)
        # Zones reload too.
        self.assertEqual(revived.zone_of("binary_sensor.front_door"), ZONE_ENTRY)

    def test_reboot_mid_pending_fails_secure(self):
        self._seed_zones()
        self.engine.arm(MODE_AWAY, exit_delay=0)
        self.engine.process_sensor("binary_sensor.front_door", True)
        self.assertEqual(self.engine.snapshot()["mode"], MODE_PENDING)
        # The countdown timer dies with the process — a revived hub must not
        # sit waiting forever. It fails secure to triggered.
        revived = self._make_engine()
        self.assertEqual(revived.snapshot()["mode"], MODE_TRIGGERED)
        self.assertEqual(len(self.alerts), 1)

    # -- default-delay settings ------------------------------------------------

    def test_settings_default_to_constants(self):
        self.assertEqual(
            self.engine.get_settings(),
            {
                "entry_delay": DEFAULT_ENTRY_DELAY_SECONDS,
                "exit_delay": DEFAULT_EXIT_DELAY_SECONDS,
            },
        )

    def test_set_settings_updates_both(self):
        result = self.engine.set_settings(entry_delay=15, exit_delay=45)
        self.assertEqual(result, {"entry_delay": 15, "exit_delay": 45})
        self.assertEqual(self.engine.get_settings(), {"entry_delay": 15, "exit_delay": 45})

    def test_set_settings_partial_keeps_other(self):
        self.engine.set_settings(entry_delay=15, exit_delay=45)
        self.engine.set_settings(exit_delay=90)
        self.assertEqual(
            self.engine.get_settings(), {"entry_delay": 15, "exit_delay": 90}
        )

    def test_set_settings_rejects_bad_value(self):
        with self.assertRaises(AlarmError):
            self.engine.set_settings(entry_delay=-5)
        with self.assertRaises(AlarmError):
            self.engine.set_settings(exit_delay=99999)
        # A rejected write leaves the stored value untouched.
        self.assertEqual(
            self.engine.get_settings()["entry_delay"], DEFAULT_ENTRY_DELAY_SECONDS
        )

    def test_settings_survive_reboot(self):
        self.engine.set_settings(entry_delay=12, exit_delay=34)
        revived = self._make_engine()
        self.assertEqual(
            revived.get_settings(), {"entry_delay": 12, "exit_delay": 34}
        )

    def test_arm_falls_back_to_stored_defaults(self):
        self.engine.set_settings(entry_delay=12, exit_delay=0)
        self._seed_zones()
        # No delays passed -> the engine uses the stored defaults, not the
        # module constants. exit_delay=0 means the arm is live immediately, so
        # an entry trip starts the 12s countdown we configured.
        self.engine.arm(MODE_AWAY)
        self.engine.process_sensor("binary_sensor.front_door", True)
        snap = self.engine.snapshot()
        self.assertEqual(snap["mode"], MODE_PENDING)
        self.assertEqual(snap["pending_until"], self.clock.t + 12)

    # -- history ---------------------------------------------------------------

    def test_history_newest_first_and_limited(self):
        self.engine.arm(MODE_AWAY)
        self.engine.disarm()
        self.engine.arm(MODE_HOME)
        recent = self.engine.history(limit=2)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["kind"], EVENT_ARMED)
        self.assertEqual(recent[0]["mode"], MODE_HOME)

    def test_history_limit_validated(self):
        with self.assertRaises(AlarmError):
            self.engine.history(limit=0)

    # -- alert sink isolation --------------------------------------------------

    def test_bad_alert_sink_does_not_break_alarm(self):
        def boom(_event):
            raise RuntimeError("push backend down")

        engine = AlarmEngine(
            self.storage.table("alarm_state2"),
            self.storage.table("alarm_zones2"),
            self.storage.table("alarm_history2"),
            self.storage.table("alarm_settings2"),
            alert_sink=boom,
            clock=self.clock,
        )
        engine.warm_up()
        engine.set_zone("binary_sensor.smoke", ZONE_LIFE_SAFETY, "Smoke")
        # Sink raises, but the state machine still transitions correctly.
        event = engine.process_sensor("binary_sensor.smoke", True)
        self.assertEqual(event["kind"], EVENT_LIFE_SAFETY)
        self.assertEqual(engine.snapshot()["mode"], MODE_TRIGGERED)


if __name__ == "__main__":
    unittest.main()
