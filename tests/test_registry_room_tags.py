"""Room-tag persistence and room-deletion safety contracts."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "casasmart"
sys.path.insert(0, str(_COMPONENT_DIR))
_SPEC = importlib.util.spec_from_file_location(
    "casasmart_registry", _COMPONENT_DIR / "registry.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_REGISTRY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_REGISTRY)


class Table(dict):
    """In-memory KeyValueTable equivalent for pure engine tests."""


def make_engine(*, rooms=None, devices=None, tags=None):
    return _REGISTRY.RegistryEngine(
        Table(),
        rooms if rooms is not None else Table(),
        devices if devices is not None else Table(),
        Table(),
        Table(),
        Table(),
        tags if tags is not None else Table(),
    )


class RegistryRoomTagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rooms = Table(
            {
                "living": {
                    "name": "Living",
                    "floor_id": None,
                    "icon": None,
                    "sort_order": 0,
                },
                "bedroom": {
                    "name": "Bedroom",
                    "floor_id": None,
                    "icon": None,
                    "sort_order": 1,
                },
            }
        )
        self.tags = Table()
        self.engine = make_engine(rooms=self.rooms, tags=self.tags)

    def test_multi_room_crud_persists_and_assignments_are_exclusive(self) -> None:
        family = self.engine.create_room_tag("Family", "#2563eb", ["living", "bedroom"])
        self.assertEqual(family["color"], "#2563EB")
        self.assertEqual(family["room_ids"], ["living", "bedroom"])

        quiet = self.engine.create_room_tag("Quiet", "#7C3AED", ["bedroom"])
        listed = {tag["tag_id"]: tag for tag in self.engine.list_room_tags()}
        self.assertEqual(listed[family["tag_id"]]["room_ids"], ["living"])
        self.assertEqual(listed[quiet["tag_id"]]["room_ids"], ["bedroom"])

        updated = self.engine.update_room_tag(
            family["tag_id"], name="Common", color="#0F766E", room_ids=["living"]
        )
        self.assertEqual(updated["name"], "Common")
        reloaded = make_engine(rooms=self.rooms, tags=self.tags)
        self.assertEqual(reloaded.list_room_tags(), self.engine.list_room_tags())

        self.engine.delete_room_tag(quiet["tag_id"])
        self.assertEqual(
            [tag["tag_id"] for tag in self.engine.list_room_tags()],
            [family["tag_id"]],
        )

    def test_old_storage_without_tags_loads_as_empty(self) -> None:
        self.assertEqual(self.engine.list_room_tags(), [])
        self.assertEqual(self.tags, {})

    def test_corrupt_legacy_tag_rows_are_sanitized_without_breaking_snapshot(self) -> None:
        self.tags["all"] = {
            "missing-name": {"color": "not-a-color", "room_ids": "living"},
            "safe": {
                "name": "  Family  ",
                "color": "not-a-color",
                "room_ids": ["living", "living", 7, "ghost"],
            },
        }

        self.assertEqual(
            self.engine.list_room_tags(),
            [
                {
                    "tag_id": "safe",
                    "name": "Family",
                    "color": "#475569",
                    "room_ids": ["living"],
                }
            ],
        )

    def test_validation_rejects_bad_or_unsafe_tag_data(self) -> None:
        with self.assertRaisesRegex(_REGISTRY.RegistryError, "allowed preset"):
            self.engine.create_room_tag("Bad", "#00FF00", ["living"])
        with self.assertRaisesRegex(_REGISTRY.RegistryError, "at least one"):
            self.engine.create_room_tag("Empty", "#2563EB", [])
        with self.assertRaisesRegex(_REGISTRY.RegistryError, "Unknown room_id"):
            self.engine.create_room_tag("Ghost", "#2563EB", ["ghost"])
        self.engine.create_room_tag("Family", "#2563EB", ["living"])
        with self.assertRaisesRegex(_REGISTRY.RegistryError, "already exists"):
            self.engine.create_room_tag(" family ", "#EA580C", ["bedroom"])

    def test_room_delete_unassigns_without_losing_device_metadata(self) -> None:
        devices = Table(
            {
                "light.one": {
                    "room_id": "living",
                    "display_name": "Reading Lamp",
                    "sort_order": 7,
                    "future_metadata": {"kept": True},
                },
                "switch.two": {
                    "room_id": "bedroom",
                    "display_name": None,
                    "sort_order": 8,
                },
            }
        )
        engine = make_engine(rooms=self.rooms, devices=devices, tags=self.tags)
        tag = engine.create_room_tag("Family", "#EA580C", ["living", "bedroom"])

        self.assertEqual(engine.delete_room("living"), 1)
        self.assertEqual(devices["light.one"]["room_id"], None)
        self.assertEqual(devices["light.one"]["display_name"], "Reading Lamp")
        self.assertEqual(devices["light.one"]["sort_order"], 7)
        self.assertEqual(devices["light.one"]["future_metadata"], {"kept": True})
        self.assertIn("switch.two", devices)
        self.assertEqual(engine.list_room_tags()[0]["room_ids"], ["bedroom"])

        engine.delete_room("bedroom")
        self.assertEqual(engine.list_room_tags(), [])
        self.assertNotIn(tag["tag_id"], self.tags.get("all", {}))


if __name__ == "__main__":
    unittest.main()
