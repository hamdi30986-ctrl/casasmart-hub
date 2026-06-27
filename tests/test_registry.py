"""Unit tests for B17: the device-registry engine.

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

from registry import (  # noqa: E402
    UNSET,
    InUseError,
    RegistryEngine,
    RegistryError,
    UnknownItemError,
)
from storage import HubStorage  # noqa: E402


def make_engine(storage: HubStorage) -> RegistryEngine:
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


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.storage.close)
        self.engine = make_engine(self.storage)


class FloorTests(RegistryTestCase):
    def test_crud(self):
        floor = self.engine.create_floor("  Ground  ", 1)
        self.assertEqual(floor["name"], "Ground")
        self.assertEqual(floor["sort_order"], 1)
        updated = self.engine.update_floor(floor["floor_id"], name="GF")
        self.assertEqual(updated["name"], "GF")
        self.assertEqual(updated["sort_order"], 1)  # untouched
        self.engine.delete_floor(floor["floor_id"])
        self.assertEqual(self.engine.list_floors(), [])

    def test_validation(self):
        with self.assertRaises(RegistryError):
            self.engine.create_floor("")
        with self.assertRaises(RegistryError):
            self.engine.create_floor("x" * 65)
        with self.assertRaises(RegistryError):
            self.engine.create_floor("ok", sort_order="first")
        with self.assertRaises(RegistryError):
            self.engine.create_floor("ok", sort_order=True)  # bool is not an order
        with self.assertRaises(UnknownItemError):
            self.engine.update_floor("floor-nope", name="x")

    def test_delete_refused_while_rooms_reference_it(self):
        floor = self.engine.create_floor("First")
        self.engine.create_room("Bedroom", floor_id=floor["floor_id"])
        with self.assertRaises(InUseError):
            self.engine.delete_floor(floor["floor_id"])


class RoomTests(RegistryTestCase):
    def test_crud_and_name_mirror(self):
        room = self.engine.create_room("Living", icon="sofa")
        self.assertEqual(self.engine.room_name(room["room_id"]), "Living")
        self.engine.update_room(room["room_id"], name="Lounge")
        self.assertEqual(self.engine.room_name(room["room_id"]), "Lounge")
        # icon sentinel: explicit None clears, absent leaves alone
        kept = self.engine.update_room(room["room_id"], sort_order=3)
        self.assertEqual(kept["icon"], "sofa")
        cleared = self.engine.update_room(room["room_id"], icon=None)
        self.assertIsNone(cleared["icon"])

    def test_unknown_floor_refused(self):
        with self.assertRaises(RegistryError):
            self.engine.create_room("Living", floor_id="floor-nope")

    def test_delete_cascades_to_unassigned(self):
        room = self.engine.create_room("Living")
        self.engine.assign_device("light.sofa", room_id=room["room_id"])
        cleared = self.engine.delete_room(room["room_id"])
        self.assertEqual(cleared, 1)
        # Explicit Unassigned — NOT the fallback sentinel.
        self.assertIsNone(self.engine.room_of("light.sofa"))
        self.assertIsNone(self.engine.room_name(room["room_id"]))


class AssignmentTests(RegistryTestCase):
    def test_room_of_sentinel_semantics(self):
        # No record at all -> UNSET (filtering falls back to the HA area).
        self.assertIs(self.engine.room_of("light.unknown"), UNSET)
        room = self.engine.create_room("Living")
        self.engine.assign_device("light.sofa", room_id=room["room_id"])
        self.assertEqual(self.engine.room_of("light.sofa"), room["room_id"])
        # Explicit Unassigned -> None (fallback must NOT kick in).
        self.engine.assign_device("light.sofa", room_id=None)
        self.assertIsNone(self.engine.room_of("light.sofa"))
        # Dropping the record reverts to the fallback sentinel.
        self.engine.remove_assignment("light.sofa")
        self.assertIs(self.engine.room_of("light.sofa"), UNSET)

    def test_display_name_mirror(self):
        self.engine.assign_device("light.sofa", display_name="Sofa Lamp")
        self.assertEqual(self.engine.display_name_of("light.sofa"), "Sofa Lamp")
        # Sentinel: untouched fields stay.
        self.engine.assign_device("light.sofa", sort_order=5)
        self.assertEqual(self.engine.display_name_of("light.sofa"), "Sofa Lamp")
        self.engine.assign_device("light.sofa", display_name=None)
        self.assertIsNone(self.engine.display_name_of("light.sofa"))

    def test_validation(self):
        with self.assertRaises(RegistryError):
            self.engine.assign_device("not-an-entity-id")
        with self.assertRaises(RegistryError):
            self.engine.assign_device("light.sofa", room_id="room-nope")
        with self.assertRaises(UnknownItemError):
            self.engine.remove_assignment("light.never_assigned")


class UserDeviceTests(RegistryTestCase):
    def test_upsert_get_list(self):
        dev = self.engine.upsert_user_device(
            "dev-1",
            entity_ids=["switch.gang_left", "switch.gang_right"],
            gang_types={"left": "light", "right": "switch"},
            gang_names={"left": "Lounge"},
            config_entity_ids=["switch.gang_led"],
            device_type="wallSwitch",
            custom_name="Lounge wall",
        )
        self.assertEqual(dev["ha_device_id"], "dev-1")
        self.assertEqual(dev["gang_types"], {"left": "light", "right": "switch"})
        self.assertEqual(
            self.engine.get_user_device("dev-1")["custom_name"], "Lounge wall"
        )
        self.assertEqual(len(self.engine.list_user_devices()), 1)

    def test_grabbed_includes_primaries_and_config(self):
        self.engine.upsert_user_device(
            "dev-1", entity_ids=["switch.a"], config_entity_ids=["switch.a_led"]
        )
        self.assertEqual(
            self.engine.grabbed_entity_ids(), {"switch.a", "switch.a_led"}
        )

    def test_grabbed_keeps_every_gang_of_a_multi_gang_device(self):
        # All gangs + config of one physical device count as grabbed — an
        # offline gang is still grabbed, so the add-list keeps it subtracted.
        self.engine.upsert_user_device(
            "dev-1",
            entity_ids=["switch.left", "switch.middle", "switch.right"],
            config_entity_ids=["switch.left_led"],
        )
        self.assertEqual(
            self.engine.grabbed_entity_ids(),
            {"switch.left", "switch.middle", "switch.right", "switch.left_led"},
        )

    def test_patch_leaves_unspecified_fields(self):
        self.engine.upsert_user_device(
            "dev-1",
            entity_ids=["switch.a"],
            custom_name="Old",
            device_type="wallSwitch",
        )
        patched = self.engine.patch_user_device("dev-1", custom_name="New")
        self.assertEqual(patched["custom_name"], "New")
        self.assertEqual(patched["device_type"], "wallSwitch")  # untouched
        self.assertEqual(patched["entity_ids"], ["switch.a"])  # untouched

    def test_delete_ungrabs(self):
        self.engine.upsert_user_device("dev-1", entity_ids=["switch.a"])
        self.engine.delete_user_device("dev-1")
        self.assertEqual(self.engine.grabbed_entity_ids(), set())
        with self.assertRaises(UnknownItemError):
            self.engine.get_user_device("dev-1")

    def test_survives_reload(self):
        self.engine.upsert_user_device(
            "dev-1", entity_ids=["switch.a"], gang_types={"left": "fan"}
        )
        reloaded = make_engine(self.storage)
        self.assertEqual(
            reloaded.get_user_device("dev-1")["gang_types"], {"left": "fan"}
        )

    def test_validation(self):
        with self.assertRaises(RegistryError):
            self.engine.upsert_user_device("dev-1", entity_ids=["not-an-entity"])
        with self.assertRaises(RegistryError):
            self.engine.upsert_user_device("", entity_ids=["switch.a"])
        with self.assertRaises(RegistryError):
            self.engine.upsert_user_device(
                "dev-1", entity_ids=["switch.a"], gang_types={"left": 5}
            )
        with self.assertRaises(UnknownItemError):
            self.engine.patch_user_device("nope", custom_name="x")


class SceneTests(RegistryTestCase):
    GOOD = [{"entity_id": "light.sofa", "action": "turn_on",
             "data": {"brightness": 128}}]

    def test_crud(self):
        scene = self.engine.create_scene("Movie", self.GOOD, icon="film")
        fetched = self.engine.get_scene(scene["scene_id"])
        self.assertEqual(fetched["entities"][0]["data"], {"brightness": 128})
        self.engine.update_scene(scene["scene_id"], name="Movie Night")
        self.assertEqual(
            self.engine.get_scene(scene["scene_id"])["name"], "Movie Night"
        )
        self.engine.delete_scene(scene["scene_id"])
        with self.assertRaises(UnknownItemError):
            self.engine.get_scene(scene["scene_id"])

    def test_favorite_defaults_false_persists_and_validates(self):
        scene = self.engine.create_scene("Movie", self.GOOD)
        # New scenes default to not-favorite.
        self.assertIs(scene["favorite"], False)
        self.assertIs(
            self.engine.get_scene(scene["scene_id"])["favorite"], False
        )
        # PATCHing favorite=True persists and echoes the flag.
        updated = self.engine.update_scene(scene["scene_id"], favorite=True)
        self.assertIs(updated["favorite"], True)
        self.assertIs(
            self.engine.get_scene(scene["scene_id"])["favorite"], True
        )
        # Sentinel default leaves favorite untouched.
        kept = self.engine.update_scene(scene["scene_id"], name="Movie Night")
        self.assertIs(kept["favorite"], True)
        # And it can be turned back off.
        self.assertIs(
            self.engine.update_scene(scene["scene_id"], favorite=False)[
                "favorite"
            ],
            False,
        )
        # Non-bool favorite is rejected (1 is not True; bool only).
        for bad in (1, 0, "true", None):
            with self.assertRaises(RegistryError, msg=repr(bad)):
                self.engine.update_scene(scene["scene_id"], favorite=bad)

    def test_favorite_survives_reload(self):
        scene = self.engine.create_scene("Movie", self.GOOD)
        self.engine.update_scene(scene["scene_id"], favorite=True)
        reloaded = make_engine(self.storage)
        self.assertIs(
            reloaded.get_scene(scene["scene_id"])["favorite"], True
        )

    def test_commands_validated_against_the_bridge_whitelist(self):
        cases = [
            [{"entity_id": "light.sofa", "action": "explode", "data": {}}],
            [{"entity_id": "sensor.temp", "action": "turn_on", "data": {}}],
            # 'effect' became legal in stage 3a — 'flash' is still banned.
            [{"entity_id": "light.sofa", "action": "turn_on",
              "data": {"flash": "long"}}],
            [],
            "not-a-list",
        ]
        for entities in cases:
            with self.assertRaises(RegistryError, msg=repr(entities)):
                self.engine.create_scene("Bad", entities)


class FavoritesTests(RegistryTestCase):
    def test_replace_dedupe_and_isolation(self):
        saved = self.engine.set_favorites(
            "dev-a", ["light.a", "light.b", "light.a"]
        )
        self.assertEqual(saved, ["light.a", "light.b"])  # order kept, dupe dropped
        self.engine.set_favorites("dev-b", ["light.c"])
        self.assertEqual(self.engine.get_favorites("dev-a"),
                         ["light.a", "light.b"])
        self.assertEqual(self.engine.get_favorites("dev-c"), [])

    def test_validation(self):
        with self.assertRaises(RegistryError):
            self.engine.set_favorites("dev-a", "light.a")
        with self.assertRaises(RegistryError):
            self.engine.set_favorites("dev-a", ["no-dot"])
        with self.assertRaises(RegistryError):
            self.engine.set_favorites("dev-a", [f"light.l{i}" for i in range(201)])

    def test_delete_favorites_drops_the_row(self):
        self.engine.set_favorites("mem-a", ["light.a", "light.b"])
        self.engine.delete_favorites("mem-a")
        self.assertEqual(self.engine.get_favorites("mem-a"), [])
        self.engine.delete_favorites("mem-a")  # no-op when already absent


class ImportTests(RegistryTestCase):
    FLOORS = [{"floor_id": "first_floor", "name": "First", "sort_order": 1}]
    ROOMS = [
        {"room_id": "living_room", "name": "Living", "floor_id": "first_floor"},
        {"room_id": "orphan", "name": "Orphan", "floor_id": "floor-gone"},
    ]
    ASSIGNMENTS = [
        {"entity_id": "light.sofa", "room_id": "living_room"},
        {"entity_id": "light.ghost", "room_id": "room-gone"},
    ]

    def test_import_keeps_ha_ids_and_skips_broken_refs(self):
        counts = self.engine.import_initial(self.FLOORS, self.ROOMS,
                                            self.ASSIGNMENTS)
        self.assertEqual(counts, {"floors": 1, "rooms": 2, "assignments": 1})
        # HA ids preserved -> existing room-scope claims keep working.
        self.assertEqual(self.engine.room_of("light.sofa"), "living_room")
        # Unknown floor ref -> room kept, floor cleared; unknown room ref
        # -> assignment skipped (derived Unassigned pool covers it).
        rooms = {r["room_id"]: r for r in self.engine.list_rooms()}
        self.assertIsNone(rooms["orphan"]["floor_id"])
        self.assertIs(self.engine.room_of("light.ghost"), UNSET)

    def test_import_is_lenient_about_ha_input(self):
        """A weird HA name/icon/sort must never raise — a rejection here
        would abort integration setup on every restart (audit H1)."""
        counts = self.engine.import_initial(
            [{"floor_id": "f1", "name": "x" * 500, "sort_order": "huh"}],
            [
                {"room_id": "r1", "name": "  ", "floor_id": "f1",
                 "icon": "m" * 500},
                {"room_id": "r2", "name": None, "floor_id": "f1"},
            ],
            [],
        )
        self.assertEqual(counts["floors"], 1)
        self.assertEqual(counts["rooms"], 2)
        floors = {f["floor_id"]: f for f in self.engine.list_floors()}
        rooms = {r["room_id"]: r for r in self.engine.list_rooms()}
        self.assertEqual(len(floors["f1"]["name"]), 64)  # truncated
        self.assertEqual(floors["f1"]["sort_order"], 0)  # bad type -> 0
        self.assertEqual(rooms["r1"]["name"], "r1")  # blank -> id fallback
        self.assertIsNone(rooms["r1"]["icon"])  # oversized icon dropped
        self.assertEqual(rooms["r2"]["name"], "r2")  # None -> id fallback

    def test_rerun_never_overwrites_installer_edits(self):
        self.engine.import_initial(self.FLOORS, self.ROOMS, self.ASSIGNMENTS)
        other = self.engine.create_room("Den")
        self.engine.assign_device("light.sofa", room_id=other["room_id"])
        self.engine.update_room("living_room", name="Salon")
        counts = self.engine.import_initial(self.FLOORS, self.ROOMS,
                                            self.ASSIGNMENTS)
        self.assertEqual(counts, {"floors": 0, "rooms": 0, "assignments": 0})
        self.assertEqual(self.engine.room_of("light.sofa"), other["room_id"])
        self.assertEqual(self.engine.room_name("living_room"), "Salon")


class PatchSemanticsTests(RegistryTestCase):
    def test_explicit_null_name_is_rejected_not_ignored(self):
        """`{"name": null}` must 400, not silently no-op (audit L5)."""
        floor = self.engine.create_floor("First")
        room = self.engine.create_room("Living")
        scene = self.engine.create_scene("Movie", SceneTests.GOOD)
        with self.assertRaises(RegistryError):
            self.engine.update_floor(floor["floor_id"], name=None)
        with self.assertRaises(RegistryError):
            self.engine.update_room(room["room_id"], name=None)
        with self.assertRaises(RegistryError):
            self.engine.update_scene(scene["scene_id"], name=None)
        # ... and the sentinel default really does leave fields alone.
        kept = self.engine.update_floor(floor["floor_id"], sort_order=5)
        self.assertEqual(kept["name"], "First")
        self.assertEqual(kept["sort_order"], 5)


class PersistenceTests(RegistryTestCase):
    def test_everything_survives_an_engine_reload(self):
        floor = self.engine.create_floor("First")
        room = self.engine.create_room("Living", floor_id=floor["floor_id"])
        self.engine.assign_device(
            "light.sofa", room_id=room["room_id"], display_name="Sofa Lamp"
        )
        scene = self.engine.create_scene("Movie", SceneTests.GOOD)
        self.engine.set_favorites("dev-a", ["light.sofa"])

        reloaded = make_engine(self.storage)  # fresh engine, same tables
        self.assertEqual(reloaded.room_of("light.sofa"), room["room_id"])
        self.assertEqual(reloaded.display_name_of("light.sofa"), "Sofa Lamp")
        self.assertEqual(reloaded.room_name(room["room_id"]), "Living")
        self.assertEqual(reloaded.get_scene(scene["scene_id"])["name"], "Movie")
        self.assertEqual(reloaded.get_favorites("dev-a"), ["light.sofa"])


if __name__ == "__main__":
    unittest.main()
