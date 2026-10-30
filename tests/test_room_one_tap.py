"""room_intel.one_tap: what one tap "on" turns on, and each room's last-on set.

Runs without Home Assistant (the module imports nothing). The recorder that
feeds LastOnSets and the room command that runs the plan are tested against
a real Home Assistant in test_room_last_on_ha and test_room_one_tap_ha.
"""

import importlib.util
import unittest
from datetime import UTC, datetime
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"

# Loaded by path: the module imports nothing from the package or from HA.
_spec = importlib.util.spec_from_file_location(
    "room_one_tap_rules", _PKG / "room_intel" / "one_tap.py"
)
one_tap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(one_tap)

NOW = 1_800_000_000
CAPTURE = one_tap.SOURCE_CAPTURE
LAST_ON = one_tap.SOURCE_LAST_ON
PROFILE = one_tap.SOURCE_START_PROFILE


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


class PlanTests(unittest.TestCase):
    DEVICES = {"light.a": "off", "light.b": "off", "fan.c": "off"}

    def plan(self, capture=(), last_on=(), profile=(), devices=None):
        return one_tap.plan_room_on(
            [(CAPTURE, capture), (LAST_ON, last_on), (PROFILE, profile)],
            self.DEVICES if devices is None else devices,
        )

    def test_the_capture_comes_first_then_the_last_on_set_then_the_profile(self):
        both = self.plan(["light.a"], ["light.b"], ["fan.c"])
        self.assertEqual((both.source, both.targets), (CAPTURE, ["light.a"]))
        last_on = self.plan([], ["light.b"], ["fan.c"])
        self.assertEqual((last_on.source, last_on.targets), (LAST_ON, ["light.b"]))
        profile = self.plan([], [], ["fan.c", "light.a"])
        self.assertEqual(
            (profile.source, profile.targets), (PROFILE, ["fan.c", "light.a"])
        )

    def test_with_nothing_saved_there_is_nothing_to_turn_on(self):
        plan = self.plan()
        self.assertEqual(plan, one_tap.RoomOnPlan(None, [], [], "nothing_saved"))
        self.assertEqual(
            one_tap.one_tap_summary(plan),
            {"action": "open_room", "reason": "nothing_saved"},
        )

    def test_only_lights_and_fans_ever_come_on(self):
        devices = {
            "light.a": "off",
            "fan.c": "off",
            # Even if a caller passed them as the room's devices.
            "switch.outlet": "off",
            "switch.water_heater": "off",
            "climate.ac": "off",
            "water_heater.tank": "off",
            "cover.curtain": "closed",
            "media_player.tv": "off",
        }
        plan = self.plan(capture=list(devices), devices=devices)
        self.assertEqual(plan.targets, ["light.a", "fan.c"])
        only_others = self.plan(
            capture=["switch.outlet", "climate.ac"],
            last_on=["cover.curtain", "water_heater.tank"],
            profile=["media_player.tv"],
            devices=devices,
        )
        self.assertIsNone(only_others.source)
        for domain in ("light", "fan"):
            self.assertTrue(one_tap.is_on_domain(f"{domain}.x"))
        for domain in ("switch", "climate", "water_heater", "cover", "media_player"):
            self.assertFalse(one_tap.is_on_domain(f"{domain}.x"))

    def test_a_switch_comes_on_only_as_a_named_wall_relay(self):
        # A wall relay the installer typed as a light or fan is one, when
        # the caller names it in kinds; a plug passed without a kind never.
        devices = {
            "light.a": "off",
            "switch.relay": "off",
            "switch.fan_relay": "off",
            "switch.plug": "off",
        }
        kinds = {"light.a": "light", "switch.relay": "light", "switch.fan_relay": "fan"}
        plan = one_tap.plan_room_on(
            [(LAST_ON, ["switch.plug", "switch.fan_relay", "switch.relay", "light.a"])],
            devices,
            kinds,
        )
        self.assertEqual(plan.targets, ["switch.fan_relay", "switch.relay", "light.a"])
        self.assertEqual(
            one_tap.one_tap_summary(plan, kinds),
            {
                "action": "turn_on",
                "source": LAST_ON,
                "light_count": 2,
                "fan_count": 1,
                "unavailable_count": 0,
            },
        )
        only_plug = one_tap.plan_room_on([(LAST_ON, ["switch.plug"])], devices, kinds)
        self.assertIsNone(only_plug.source)
        self.assertTrue(one_tap.may_come_on("switch.relay", kinds))
        self.assertFalse(one_tap.may_come_on("switch.plug", kinds))
        self.assertFalse(one_tap.may_come_on("climate.ac", {"climate.ac": "light"}))

    def test_devices_gone_from_the_room_are_left_out(self):
        # Deleted, moved or unapproved: not among the room's devices.
        plan = self.plan(last_on=["light.deleted", "light.a", "light.moved"])
        self.assertEqual((plan.source, plan.targets), (LAST_ON, ["light.a"]))
        self.assertEqual(plan.unavailable, [])
        # A capture with nothing of the room's left falls through.
        plan = self.plan(capture=["light.moved"], last_on=["light.b"])
        self.assertEqual(plan.source, LAST_ON)

    def test_unavailable_devices_are_skipped_and_reported(self):
        devices = {**self.DEVICES, "light.b": "unavailable", "fan.c": "unknown"}
        plan = self.plan(last_on=["light.a", "light.b", "fan.c"], devices=devices)
        self.assertEqual(plan.targets, ["light.a"])
        self.assertEqual(plan.unavailable, ["light.b", "fan.c"])
        self.assertEqual(
            one_tap.one_tap_summary(plan),
            {
                "action": "turn_on",
                "source": "last_on",
                "light_count": 1,
                "fan_count": 0,
                "unavailable_count": 2,
            },
        )

    def test_a_source_with_only_unavailable_devices_falls_through(self):
        devices = {**self.DEVICES, "light.a": "unavailable"}
        plan = self.plan(capture=["light.a"], last_on=["light.b"], devices=devices)
        self.assertEqual((plan.source, plan.targets), (LAST_ON, ["light.b"]))
        dead = self.plan(capture=["light.a"], devices=devices)
        self.assertEqual(dead, one_tap.RoomOnPlan(None, [], [], "unavailable"))

    def test_devices_already_on_are_in_the_plan(self):
        devices = {**self.DEVICES, "light.a": "on"}
        plan = self.plan(last_on=["light.a", "light.b"], devices=devices)
        self.assertEqual(plan.targets, ["light.a", "light.b"])

    def test_duplicates_count_once_and_keep_their_order(self):
        plan = self.plan(profile=["fan.c", "light.a", "fan.c"])
        self.assertEqual(plan.targets, ["fan.c", "light.a"])
        self.assertEqual(
            one_tap.one_tap_summary(plan),
            {
                "action": "turn_on",
                "source": "start_profile",
                "light_count": 1,
                "fan_count": 1,
                "unavailable_count": 0,
            },
        )


class CaptureAgeTests(unittest.TestCase):
    def test_a_capture_is_used_for_24_hours(self):
        day = one_tap.CAPTURE_MAX_AGE_SECONDS
        self.assertEqual(day, 86400)
        self.assertTrue(one_tap.capture_is_fresh(iso(NOW - day), NOW))
        self.assertFalse(one_tap.capture_is_fresh(iso(NOW - day - 1), NOW))
        self.assertTrue(one_tap.capture_is_fresh(iso(NOW - 60), NOW))
        # A clock that went back makes it look new: still the room's last off.
        self.assertTrue(one_tap.capture_is_fresh(iso(NOW + 60), NOW))

    def test_an_unreadable_capture_time_is_not_fresh(self):
        for value in (None, "", "yesterday", 1_800_000_000, "2026-10-23T10:00:00"):
            with self.subTest(value=value):
                self.assertFalse(one_tap.capture_is_fresh(value, NOW))

    def test_a_capture_the_room_went_dark_again_after_no_longer_counts(self):
        captured = iso(NOW - 3600)
        last_on = one_tap.LastOn

        def counts(at):
            return one_tap.capture_counts(captured, last_on(("light.a",), at), NOW)

        self.assertTrue(one_tap.capture_counts(captured, None, NOW))
        # The hub's own off: the room went dark just before the capture, or a
        # slow device reported its off a little after it.
        self.assertTrue(counts(NOW - 3600 - 2))
        self.assertTrue(counts(NOW - 3600 + 120))
        # Lit again later and switched off at the wall: that is "last time".
        self.assertFalse(counts(NOW - 3600 + 121))
        self.assertFalse(
            one_tap.capture_counts(iso(NOW - 90000), last_on(("light.a",), 0), NOW)
        )


class PlanRoomTests(unittest.TestCase):
    DEVICES = {"light.a": "off", "light.b": "off", "fan.c": "off"}

    def plan(self, capture, last_on=None, profile=(), acs=None):
        return one_tap.plan_room(
            capture=capture,
            last_on=last_on,
            start_profile=profile,
            devices=self.DEVICES,
            now_t=NOW,
            capture_acs=acs,
        )

    def test_a_capture_brings_its_acs_back_with_its_lights(self):
        plan = self.plan((["light.a"], iso(NOW - 60)), acs={"climate.ac": "off"})
        self.assertEqual(
            (plan.source, plan.targets, plan.acs),
            (CAPTURE, ["light.a"], ("climate.ac",)),
        )
        self.assertEqual(
            one_tap.one_tap_summary(plan),
            {
                "action": "turn_on",
                "source": CAPTURE,
                "light_count": 1,
                "fan_count": 0,
                "unavailable_count": 0,
                "ac_count": 1,
            },
        )

    def test_an_ac_only_capture_beats_the_last_on_set(self):
        # An off that found only the AC running: the lights stay off.
        plan = self.plan(
            ([], iso(NOW - 60)),
            one_tap.LastOn(("light.b",), NOW - 3600),
            ["fan.c"],
            {"climate.ac": "off"},
        )
        self.assertEqual(
            (plan.source, plan.targets, plan.acs), (CAPTURE, [], ("climate.ac",))
        )

    def test_no_counted_capture_means_no_ac(self):
        for captured, last_at in (
            (iso(NOW - 86401), NOW - 86402),  # older than 24 h
            (iso(NOW - 3600), NOW - 60),  # the room went dark again since
        ):
            with self.subTest(captured=captured):
                plan = self.plan(
                    ([], captured),
                    one_tap.LastOn(("light.b",), last_at),
                    acs={"climate.ac": "off"},
                )
                self.assertEqual(
                    (plan.source, plan.targets, plan.acs), (LAST_ON, ["light.b"], ())
                )
        self.assertIsNone(
            self.plan(([], iso(NOW - 90000)), acs={"climate.ac": "off"}).source
        )

    def test_an_offline_ac_is_reported_not_counted(self):
        plan = self.plan(
            (["light.a"], iso(NOW - 60)),
            acs={"climate.ac": "unavailable", "climate.b": "off"},
        )
        self.assertEqual(
            (plan.source, plan.targets, plan.unavailable, plan.acs),
            (CAPTURE, ["light.a"], ["climate.ac"], ("climate.b",)),
        )
        # Lights only in the plan, the capture's AC offline.
        plan = self.plan(
            (["light.a"], iso(NOW - 60)), acs={"climate.ac": "unavailable"}
        )
        self.assertEqual(
            (plan.source, plan.targets, plan.unavailable, plan.acs),
            (CAPTURE, ["light.a"], ["climate.ac"], ()),
        )
        # Nothing else: there is nothing to turn on because it is offline.
        plan = self.plan(([], iso(NOW - 60)), acs={"climate.ac": "unavailable"})
        self.assertEqual((plan.source, plan.reason), (None, one_tap.REASON_UNAVAILABLE))
        # The next source wins, without the AC.
        plan = self.plan(
            ([], iso(NOW - 60)),
            one_tap.LastOn(("light.b",), NOW - 3600),
            acs={"climate.ac": "unavailable"},
        )
        self.assertEqual(
            (plan.source, plan.targets, plan.unavailable), (LAST_ON, ["light.b"], [])
        )

    def test_a_current_capture_beats_the_last_on_set(self):
        plan = self.plan(
            (["light.a"], iso(NOW - 60)), one_tap.LastOn(("light.b",), NOW - 61)
        )
        self.assertEqual((plan.source, plan.targets), (CAPTURE, ["light.a"]))

    def test_an_old_or_outdated_capture_gives_way_to_the_last_on_set(self):
        for captured, last_at in (
            (iso(NOW - 86401), NOW - 86402),  # older than 24 h
            (iso(NOW - 3600), NOW - 60),  # the room went dark again since
        ):
            with self.subTest(captured=captured):
                plan = self.plan(
                    (["light.a"], captured), one_tap.LastOn(("light.b",), last_at)
                )
                self.assertEqual((plan.source, plan.targets), (LAST_ON, ["light.b"]))

    def test_the_start_profile_when_nothing_is_known(self):
        plan = self.plan(([], None), None, ["fan.c"])
        self.assertEqual((plan.source, plan.targets), (PROFILE, ["fan.c"]))
        # An old capture alone is not known either.
        plan = self.plan((["light.a"], iso(NOW - 90000)), None, ["fan.c"])
        self.assertEqual(plan.source, PROFILE)
        self.assertIsNone(self.plan((["light.a"], iso(NOW - 90000))).source)


class LastOnTests(unittest.TestCase):
    def setUp(self):
        self.sets = one_tap.LastOnSets()

    def on(self, eid, room="kitchen", t=NOW):
        return self.sets.update(eid, room, True, turned_off=False, t=t)

    def off(self, eid, room="kitchen", t=NOW):
        return self.sets.update(eid, room, False, turned_off=True, t=t)

    def gone(self, eid, room="kitchen", t=NOW):
        """Unavailable, unknown or removed: not lit, not an off."""
        return self.sets.update(eid, room, False, turned_off=False, t=t)

    def test_the_lights_on_when_the_room_went_dark(self):
        self.on("light.a")
        self.on("light.b")
        self.assertFalse(self.off("light.a", t=NOW + 2))
        self.assertTrue(self.off("light.b", t=NOW + 3))
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.b", "light.a"])
        self.assertEqual(self.sets.get("kitchen").at, NOW + 3)

    def test_offs_more_than_two_minutes_before_the_last_are_not_in_it(self):
        self.on("light.a")
        self.on("light.b")
        self.on("fan.c")
        self.off("light.a", t=NOW)
        self.off("fan.c", t=NOW + 3600 - 120)
        self.off("light.b", t=NOW + 3600)
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.b", "fan.c"])

    def test_a_device_switched_back_on_is_not_counted_twice(self):
        self.on("light.a")
        self.on("light.b")
        self.off("light.a", t=NOW + 1)
        self.on("light.a", t=NOW + 2)
        self.off("light.a", t=NOW + 3)
        self.off("light.b", t=NOW + 4)
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.b", "light.a"])

    def test_an_earlier_lit_spell_does_not_join(self):
        self.on("light.a")
        self.off("light.a", t=NOW)
        self.on("light.b", t=NOW + 5)
        self.off("light.b", t=NOW + 10)
        # light.a went off 10 s earlier but in the room's previous lit spell.
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.b"])

    def test_a_room_that_goes_dark_without_an_off_keeps_its_set(self):
        self.on("light.a")
        self.off("light.a", t=NOW)
        self.on("light.b", t=NOW + 60)
        self.assertFalse(self.gone("light.b", t=NOW + 70))
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.a"])
        self.assertEqual(self.sets.get("kitchen").at, NOW)

    def test_a_device_moved_while_on_is_not_an_off(self):
        self.on("light.a", room="kitchen")
        # Its next change arrives with its new room.
        self.assertFalse(self.off("light.a", room="majlis", t=NOW + 5))
        self.assertIsNone(self.sets.get("kitchen"))
        self.assertIsNone(self.sets.get("majlis"))
        self.on("light.a", room="majlis", t=NOW + 6)
        self.off("light.a", room="majlis", t=NOW + 7)
        self.assertEqual(self.sets.entity_ids("majlis"), ["light.a"])

    def test_attribute_changes_keep_the_fading_devices(self):
        self.on("light.a")
        self.on("light.b")
        self.off("light.b", t=NOW + 1)
        self.assertFalse(self.on("light.a", t=NOW + 2))  # a brightness change
        self.off("light.a", t=NOW + 3)
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.a", "light.b"])

    def test_rooms_are_followed_apart(self):
        self.on("light.a", room="kitchen")
        self.on("light.m", room="majlis")
        self.off("light.a", room="kitchen", t=NOW + 1)
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.a"])
        self.assertIsNone(self.sets.get("majlis"))

    def test_seed_starts_over_without_writing_a_set(self):
        self.sets.load([("kitchen", {"v": 1, "entity_ids": ["light.old"], "at": 5})])
        self.on("light.a")
        self.sets.seed([("light.b", "kitchen")])
        self.assertEqual(self.sets.drain(), {})
        # light.a isn't followed any more; light.b is.
        self.assertFalse(self.off("light.a", t=NOW + 1))
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.old"])
        self.assertTrue(self.off("light.b", t=NOW + 2))
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.b"])

    def test_a_reseed_while_a_plate_is_switched_off_keeps_the_gangs_already_off(
        self,
    ):
        # The recorder reseeds on every registry edit.
        for eid in ("light.a", "light.b", "light.c", "light.d"):
            self.on(eid)
        self.off("light.a", t=NOW + 1)
        self.off("light.b", t=NOW + 2)
        # A setting saved in the app: c and d still on, b back on meanwhile.
        self.sets.seed(
            [("light.c", "kitchen"), ("light.d", "kitchen"), ("light.b", "kitchen")]
        )
        self.off("light.d", t=NOW + 3)
        self.off("light.b", t=NOW + 4)
        self.assertTrue(self.off("light.c", t=NOW + 5))
        self.assertEqual(
            self.sets.entity_ids("kitchen"),
            ["light.c", "light.b", "light.d", "light.a"],
        )
        # A room dark at the reseed keeps nothing of its fading devices.
        self.on("light.m", room="majlis")
        self.on("light.n", room="majlis")
        self.off("light.m", room="majlis", t=NOW + 6)
        self.sets.seed([])
        self.on("light.n", room="majlis", t=NOW + 7)
        self.off("light.n", room="majlis", t=NOW + 8)
        self.assertEqual(self.sets.entity_ids("majlis"), ["light.n"])

    def test_drain_saves_each_changed_room_once(self):
        self.on("light.a")
        self.off("light.a", t=NOW + 1)
        self.on("light.a", t=NOW + 2)
        self.off("light.a", t=NOW + 3)
        self.assertTrue(self.sets.unsaved)
        self.assertEqual(
            self.sets.drain(),
            {"kitchen": {"v": 1, "entity_ids": ["light.a"], "at": NOW + 3}},
        )
        self.assertEqual(self.sets.drain(), {})
        self.sets.mark_unsaved(["kitchen"])
        self.assertEqual(list(self.sets.drain()), ["kitchen"])

    def test_rooms_that_left_the_registry_are_deleted(self):
        self.on("light.a", room="kitchen")
        self.off("light.a", room="kitchen")
        self.on("light.m", room="majlis")
        self.off("light.m", room="majlis")
        self.on("light.m", room="majlis")
        self.sets.drain()
        self.sets.forget_rooms_except(["kitchen"])
        self.assertIsNone(self.sets.get("majlis"))
        self.assertEqual(self.sets.drain(), {"majlis": None})
        # And it no longer follows the deleted room's lit light.
        self.assertFalse(self.off("light.m", room="majlis", t=NOW + 9))

    def test_load_takes_only_readable_docs(self):
        self.sets.load(
            [
                ("kitchen", {"v": 1, "entity_ids": ["light.a", "light.a"], "at": 7}),
                ("v2", {"v": 2, "entity_ids": ["light.a"], "at": 7}),
                ("empty", {"v": 1, "entity_ids": [], "at": 7}),
                ("bad-id", {"v": 1, "entity_ids": ["nodot"], "at": 7}),
                ("bad-at", {"v": 1, "entity_ids": ["light.a"], "at": True}),
                ("not-dict", ["light.a"]),
            ]
        )
        self.assertEqual(len(self.sets), 1)
        self.assertEqual(self.sets.entity_ids("kitchen"), ["light.a"])

    def test_clear_forgets_everything(self):
        self.on("light.a")
        self.off("light.a")
        self.on("light.b")
        self.sets.clear()
        self.assertEqual(len(self.sets), 0)
        self.assertEqual(self.sets.drain(), {})
        self.assertFalse(self.off("light.b"))


if __name__ == "__main__":
    unittest.main()
