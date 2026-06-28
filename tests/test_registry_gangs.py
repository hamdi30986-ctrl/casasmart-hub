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

from registry import RegistryEngine, RegistryError  # noqa: E402
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
             "presentation": "solo"},
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


if __name__ == "__main__":
    unittest.main()
