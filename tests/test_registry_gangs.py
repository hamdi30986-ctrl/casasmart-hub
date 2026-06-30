"""Migration Phase 1: nested gangs + presentation + control_entity_ids + room_id.

Run from the repo root:
    python3 -m unittest tests.test_registry_gangs -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"),
)

from registry import RegistryEngine, RegistryError, UnknownItemError  # noqa: E402
from storage import HubStorage  # noqa: E402


def _make_engine(storage: HubStorage) -> RegistryEngine:
    engine = RegistryEngine(
        storage.table("registry_floors"),
        storage.table("registry_rooms"),
        storage.table("registry_devices"),
        storage.table("registry_scenes"),
        storage.table("registry_favorites"),
        storage.table("registry_user_devices"),
    )
    engine.warm_up()
    return engine


class GangsPhase1Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.engine = _make_engine(self.storage)

    def test_upsert_stores_and_serves_nested_gangs(self):
        dev = self.engine.upsert_user_device(
            "dev-1",
            control_entity_ids=["switch.kitchen_left", "switch.kitchen_right"],
            gangs={
                "switch.kitchen_left": {
                    "type": "light",
                    "icon": "bulb",
                    "name": "Ceiling",
                    "presentation": "solo",
                },
                "switch.kitchen_right": {"type": "fan"},
            },
            config_entity_ids=["switch.kitchen_led"],
            room_id="kitchen",
        )
        # control_entity_ids stored as entity_ids, served under BOTH names.
        self.assertEqual(
            dev["entity_ids"], ["switch.kitchen_left", "switch.kitchen_right"]
        )
        self.assertEqual(
            dev["control_entity_ids"],
            ["switch.kitchen_left", "switch.kitchen_right"],
        )
        self.assertEqual(dev["room_id"], "kitchen")
        self.assertEqual(
            dev["gangs"]["switch.kitchen_left"],
            {"type": "light", "icon": "bulb", "name": "Ceiling",
             "presentation": "solo", "room_id": None},
        )
        right = dev["gangs"]["switch.kitchen_right"]
        self.assertEqual(right["type"], "fan")
        self.assertEqual(right["presentation"], "grouped")  # default
        self.assertIsNone(right["icon"])
        self.assertEqual(self.engine.get_user_device("dev-1")["gangs"], dev["gangs"])
        self.assertEqual(
            self.engine.list_user_devices()[0]["control_entity_ids"],
            dev["control_entity_ids"],
        )

    def test_entity_ids_alias_and_legacy_empty_gangs(self):
        dev = self.engine.upsert_user_device("d", entity_ids=["switch.a"])
        self.assertEqual(dev["entity_ids"], ["switch.a"])
        self.assertEqual(dev["control_entity_ids"], ["switch.a"])
        self.assertEqual(dev["gangs"], {})  # legacy write -> empty gangs
        self.assertIsNone(dev["room_id"])

    def test_clean_gangs_validation(self):
        bad = [
            {"switch.a": {"type": "light", "presentation": "bogus"}},
            {"switch.a": {"type": 42}},
            {"switch.a": {"icon": "x" * 65}},
            {"switch.a": "not-a-map"},
        ]
        for gangs in bad:
            with self.assertRaises(RegistryError):
                self.engine.upsert_user_device(
                    "d", entity_ids=["switch.a"], gangs=gangs
                )
        with self.assertRaises(RegistryError):
            self.engine.upsert_user_device("d", entity_ids=["switch.a"], room_id="")

    def test_patch_gangs_and_room_leaves_rest(self):
        self.engine.upsert_user_device("d", entity_ids=["switch.a", "switch.b"])
        patched = self.engine.patch_user_device(
            "d",
            gangs={"switch.a": {"type": "fan", "presentation": "solo"}},
            room_id="den",
        )
        self.assertEqual(patched["gangs"]["switch.a"]["presentation"], "solo")
        self.assertEqual(patched["room_id"], "den")
        self.assertEqual(patched["entity_ids"], ["switch.a", "switch.b"])

    def test_grabbed_reads_control_or_entity_ids(self):
        self.engine.upsert_user_device(
            "d",
            control_entity_ids=["switch.x"],
            config_entity_ids=["switch.x_led"],
        )
        self.assertEqual(
            self.engine.grabbed_entity_ids(), {"switch.x", "switch.x_led"}
        )

    def test_gangs_and_room_survive_reload(self):
        self.engine.upsert_user_device(
            "d",
            entity_ids=["switch.a"],
            gangs={"switch.a": {"type": "light", "presentation": "solo"}},
            room_id="hall",
        )
        reloaded = _make_engine(self.storage).get_user_device("d")
        self.assertEqual(reloaded["gangs"]["switch.a"]["presentation"], "solo")
        self.assertEqual(reloaded["room_id"], "hall")

    def test_serves_defaults_for_legacy_record(self):
        # A record written BEFORE P1 has no gangs/room_id/control_entity_ids
        # keys (mirrors the 25 live records). The serve helper must still emit
        # them so the P0 client never sees a missing field.
        self.engine._user_devices["legacy-dev"] = {
            "entity_ids": ["switch.x", "switch.y"],
            "gang_types": {"x": "light"},
            "gang_names": {},
            "config_entity_ids": [],
            "device_type": "wallSwitch",
            "custom_name": None,
            "custom_icon": None,
        }
        served = self.engine.get_user_device("legacy-dev")
        self.assertEqual(served["gangs"], {})
        self.assertIsNone(served["room_id"])
        self.assertEqual(served["control_entity_ids"], ["switch.x", "switch.y"])
        self.assertEqual(served["gang_types"], {"x": "light"})  # legacy kept
        listed = [
            u for u in self.engine.list_user_devices()
            if u["ha_device_id"] == "legacy-dev"
        ][0]
        self.assertEqual(listed["gangs"], {})


class GangCommandsPhase3Tests(unittest.TestCase):
    """Phase 3: per-gang presentation/type/name-icon commands + enforcement."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.engine = _make_engine(self.storage)
        # A 2-gang record, both grouped, to flip.
        self.engine.upsert_user_device(
            "grp",
            control_entity_ids=["switch.a", "switch.b"],
            gangs={
                "switch.a": {"type": "switch", "presentation": "grouped"},
                "switch.b": {"type": "light", "presentation": "grouped"},
            },
            config_entity_ids=["switch.led"],
        )

    def _pres(self, gang):
        return self.engine.get_user_device("grp")["gangs"][gang]["presentation"]

    def test_presentation_transitions_each_map_correctly(self):
        # promote: grouped -> solo
        served = self.engine.set_gang_presentation("grp", "switch.a", "solo")
        self.assertEqual(served["gangs"]["switch.a"]["presentation"], "solo")
        self.assertEqual(self._pres("switch.a"), "solo")  # persisted
        # the OTHER gang is untouched (exactly-one is per-gang, no side effects)
        self.assertEqual(self._pres("switch.b"), "grouped")
        # delete-the-solo: solo -> grouped
        self.engine.set_gang_presentation("grp", "switch.a", "grouped")
        self.assertEqual(self._pres("switch.a"), "grouped")
        # hide: any -> hidden
        self.engine.set_gang_presentation("grp", "switch.b", "hidden")
        self.assertEqual(self._pres("switch.b"), "hidden")
        # un-hide: hidden -> grouped
        self.engine.set_gang_presentation("grp", "switch.b", "grouped")
        self.assertEqual(self._pres("switch.b"), "grouped")

    def test_invalid_presentation_rejected_and_record_untouched(self):
        with self.assertRaises(RegistryError):
            self.engine.set_gang_presentation("grp", "switch.a", "bogus")
        self.assertEqual(self._pres("switch.a"), "grouped")  # unchanged

    def test_set_gang_type_known_accepted_unknown_rejected(self):
        served = self.engine.set_gang_type("grp", "switch.a", "heater")
        self.assertEqual(served["gangs"]["switch.a"]["type"], "heater")
        self.engine.set_gang_type("grp", "switch.a", "outlet")
        self.assertEqual(
            self.engine.get_user_device("grp")["gangs"]["switch.a"]["type"],
            "outlet",
        )
        for bad in ("cover", "lock", "climate", "", 42):
            with self.assertRaises(RegistryError):
                self.engine.set_gang_type("grp", "switch.a", bad)
        # rejected type left the record on its last good value.
        self.assertEqual(
            self.engine.get_user_device("grp")["gangs"]["switch.a"]["type"],
            "outlet",
        )

    def test_set_gang_name_icon(self):
        self.engine.set_gang_name_icon("grp", "switch.b", name="Reading", icon="bulb")
        g = self.engine.get_user_device("grp")["gangs"]["switch.b"]
        self.assertEqual(g["name"], "Reading")
        self.assertEqual(g["icon"], "bulb")
        # ... sentinel leaves the other field unchanged; None clears.
        self.engine.set_gang_name_icon("grp", "switch.b", name=None)
        g = self.engine.get_user_device("grp")["gangs"]["switch.b"]
        self.assertIsNone(g["name"])
        self.assertEqual(g["icon"], "bulb")  # icon untouched
        with self.assertRaises(RegistryError):
            self.engine.set_gang_name_icon("grp", "switch.b", name=42)
        with self.assertRaises(RegistryError):
            self.engine.set_gang_name_icon("grp", "switch.b", icon="x" * 65)

    def test_gang_command_on_absent_device_or_gang_is_unknown_item(self):
        for fn in (
            lambda: self.engine.set_gang_presentation("nope", "switch.a", "solo"),
            lambda: self.engine.set_gang_type("nope", "switch.a", "light"),
            lambda: self.engine.set_gang_name_icon("nope", "switch.a", name="x"),
            lambda: self.engine.set_gang_presentation("grp", "switch.ghost", "solo"),
            lambda: self.engine.set_gang_type("grp", "switch.ghost", "light"),
            lambda: self.engine.set_gang_name_icon("grp", "switch.ghost", name="x"),
        ):
            with self.assertRaises(UnknownItemError):
                fn()

    def test_legacy_empty_gangs_record_has_no_flippable_gang(self):
        # The 25 live records serve gangs:{} — every gang command must 404 until
        # the Phase-6 migration populates gangs.
        self.engine.upsert_user_device("legacy", entity_ids=["switch.z"])
        with self.assertRaises(UnknownItemError):
            self.engine.set_gang_presentation("legacy", "switch.z", "solo")

    def test_commands_never_touch_entities_or_config(self):
        before = self.engine.get_user_device("grp")
        self.engine.set_gang_presentation("grp", "switch.a", "hidden")
        after = self.engine.get_user_device("grp")
        self.assertEqual(after["entity_ids"], before["entity_ids"])
        self.assertEqual(after["config_entity_ids"], before["config_entity_ids"])

    def test_set_gang_room(self):
        # A gang carries its own room, independent of the device's room.
        self.engine.set_gang_room("grp", "switch.a", "kitchen")
        self.assertEqual(
            self.engine.get_user_device("grp")["gangs"]["switch.a"]["room_id"],
            "kitchen",
        )
        # ...and can be cleared back to None.
        self.engine.set_gang_room("grp", "switch.a", None)
        self.assertIsNone(
            self.engine.get_user_device("grp")["gangs"]["switch.a"]["room_id"]
        )

    def test_upsert_rejects_unknown_gang_type(self):
        # Phase 3 enforcement reaches the bulk write path too. Use a relay NOT in
        # the setUp "grp" record so the H2 double-grab guard doesn't fire here.
        with self.assertRaises(RegistryError):
            self.engine.upsert_user_device(
                "x", entity_ids=["switch.x"],
                gangs={"switch.x": {"type": "cover"}},
            )
        # known types still accepted.
        ok = self.engine.upsert_user_device(
            "x", entity_ids=["switch.x"],
            gangs={"switch.x": {"type": "heater"}},
        )
        self.assertEqual(ok["gangs"]["switch.x"]["type"], "heater")
        self.assertEqual(ok["gangs"]["switch.x"]["presentation"], "grouped")


if __name__ == "__main__":
    unittest.main()
