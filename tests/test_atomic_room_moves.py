"""Real SQLite contracts: logical moves, receipts and injected disk failures."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_registry_room_tags import _REGISTRY

ROOT = Path(__file__).parents[1] / "custom_components" / "casasmart"
spec = importlib.util.spec_from_file_location(
    "phase1_storage", ROOT / "storage" / "__init__.py",
    submodule_search_locations=[str(ROOT / "storage")],
)
storage_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = storage_module
spec.loader.exec_module(storage_module)


class AtomicRoomMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = storage_module.HubStorage(Path(self.tmp.name) / "hub.db")
        self.store.open()
        self.addCleanup(self.store.close)
        self.engine = _REGISTRY.RegistryEngine(*[
            self.store.table(name) for name in (
                "floors", "rooms", "devices", "scenes", "favorites", "user_devices", "tags"
            )
        ])
        self.store.table("rooms")["a"] = {"name": "A"}
        self.store.table("rooms")["b"] = {"name": "B"}
        self.engine.upsert_user_device(
            "device", entity_ids=["light.one", "cover.two", "switch.config"],
            config_entity_ids=["switch.config"], custom_name="Keep name", custom_icon="bulb",
        )
        for eid in ("light.one", "cover.two", "switch.config"):
            self.engine.assign_device(eid, room_id="a", display_name="Keep", sort_order=7)
        self.payload = {
            "ha_device_id": "device", "room_id": "b",
            "expected_rooms": {"light.one": "a", "cover.two": "a"},
            "idempotency_key": "request_1234567890",
        }

    def move(self, payload=None, **kwargs):
        return self.engine.move_device_room(
            self.store, kwargs.pop("actor", "member"), payload or self.payload,
            assignable_ids=kwargs.pop("assignable_ids", {"light.one", "cover.two"}),
            fallback_rooms={}, **kwargs,
        )

    def test_commits_all_primary_assignments_preserves_metadata_and_config(self):
        result = self.move()
        self.assertFalse(result["replayed"])
        self.assertEqual(len(result["assignments"]), 2)
        self.assertEqual(self.engine.room_of("light.one"), "b")
        self.assertEqual(self.engine.room_of("cover.two"), "b")
        self.assertEqual(self.engine.room_of("switch.config"), "a")
        self.assertEqual(self.engine.list_assignments()["light.one"]["sort_order"], 7)
        self.assertEqual(self.engine.get_user_device("device")["custom_name"], "Keep name")

    def test_disk_error_rolls_back_prior_writes_and_receipt_and_cache(self):
        original = self.store._execute_write

        def fail(sql, params=()):
            if params[:2] == ("devices", "cover.two"):
                raise sqlite3.OperationalError("injected full disk")
            return original(sql, params)

        with patch.object(self.store, "_execute_write", side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.move()
        self.assertEqual(self.engine.list_assignments()["light.one"]["room_id"], "a")
        self.assertEqual(self.engine.room_of("light.one"), "a")
        self.assertEqual(len(self.store.table("registry_room_moves")), 0)
        self.assertFalse(self.move()["replayed"])

    def test_receipt_failure_rolls_back_complete_move(self):
        original = self.store._execute_write
        def fail(sql, params=()):
            if params and params[0] == "registry_room_moves":
                raise sqlite3.OperationalError("receipt failed")
            return original(sql, params)
        with patch.object(self.store, "_execute_write", side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.move()
        self.assertEqual(self.engine.list_assignments()["cover.two"]["room_id"], "a")

    def test_restart_retains_receipt_and_does_not_reapply_later_move(self):
        self.move()
        self.store.close()
        self.store.open()
        self.engine.assign_device("light.one", room_id="a")
        replay = self.move()
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.engine.room_of("light.one"), "a")

    def test_changed_payload_same_key_is_rejected(self):
        self.move()
        with self.assertRaises(_REGISTRY.RoomMoveConflict):
            self.move({**self.payload, "room_id": None})

    def test_key_is_scoped_to_actor(self):
        self.move()
        with self.assertRaises(_REGISTRY.RoomMoveConflict):
            self.move(actor="other")

    def test_stale_source_and_membership_and_missing_entities_rejected(self):
        self.engine.assign_device("light.one", room_id=None)
        with self.assertRaises(_REGISTRY.RoomMoveConflict):
            self.move()
        self.assertEqual(self.engine.room_of("cover.two"), "a")
        with self.assertRaises(_REGISTRY.RoomMoveConflict):
            self.move({**self.payload, "expected_rooms": {"light.one": None}})
        with self.assertRaises(_REGISTRY.UnknownItemError):
            self.move(assignable_ids={"light.one"})

    def test_deleted_destination_and_scope_fail_before_writes(self):
        with self.assertRaises(_REGISTRY.UnknownItemError):
            self.move({**self.payload, "room_id": "gone"})
        for scope in (["a"], ["b"], []):
            with self.subTest(scope=scope), self.assertRaises(_REGISTRY.RoomMoveDenied):
                self.move(scope=scope)
        self.assertEqual(self.engine.room_of("light.one"), "a")

    def test_unassigned_is_explicit_and_persists(self):
        self.move({**self.payload, "room_id": None})
        self.assertIsNone(self.engine.list_assignments()["cover.two"]["room_id"])

    def test_solo_gang_moves_without_parent_assignments_or_icon_loss(self):
        self.engine.patch_user_device("device", gangs={
            "light.one": {"presentation": "solo", "type": "light"},
        })
        self.engine.set_gang_presentation("device", "light.one", "solo")
        self.engine.set_gang_name_icon("device", "light.one", icon="lamp", name="Solo")
        result = self.move({**self.payload, "gang_entity_id": "light.one",
                            "expected_rooms": {"light.one": "a"},
                            "expected_gang_override": False})
        gang = result["user_device"]["gangs"]["light.one"]
        self.assertEqual((gang["room_id"], gang["icon"], gang["name"]), ("b", "lamp", "Solo"))
        self.assertEqual(self.engine.room_of("light.one"), "a")
        self.assertEqual(self.engine.room_of("cover.two"), "a")

    def test_nested_transaction_rollback_and_outer_commit(self):
        table = self.store.table("test")
        with self.store.transaction():
            table["outer"] = 1
            try:
                with self.store.transaction():
                    table["inner"] = 2
                    raise ValueError("rollback nested")
            except ValueError:
                pass
        self.assertEqual(dict(table.items()), {"outer": 1})

    def test_two_clients_cannot_both_overwrite_the_same_source(self):
        def run(destination, key):
            try:
                self.move({**self.payload, "room_id": destination, "idempotency_key": key})
                return "saved"
            except _REGISTRY.RoomMoveConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, "b", "client_one_123456789")
            second = pool.submit(run, None, "client_two_123456789")
            self.assertCountEqual([first.result(), second.result()], ["saved", "conflict"])
        assignments = self.engine.list_assignments()
        self.assertEqual(assignments["light.one"]["room_id"], assignments["cover.two"]["room_id"])

    def test_explicit_unassigned_gang_survives_old_client_metadata_update(self):
        self.engine.patch_user_device("device", gangs={
            "light.one": {"presentation": "solo", "type": "light"},
        })
        result = self.move({**self.payload, "gang_entity_id": "light.one", "room_id": None,
                            "expected_rooms": {"light.one": "a"}, "expected_gang_override": False})
        self.assertTrue(result["user_device"]["gangs"]["light.one"]["room_override"])
        self.engine.patch_user_device("device", gangs={
            "light.one": {"presentation": "solo", "type": "light", "room_id": None, "icon": "lamp"},
        })
        gang = self.engine.get_user_device("device")["gangs"]["light.one"]
        self.assertTrue(gang["room_override"])
        self.assertIsNone(gang["room_id"])
        self.assertEqual(self.engine.room_of("light.one"), "a")

    def test_gang_inheritance_change_conflicts_even_when_both_rooms_null(self):
        self.engine.assign_device("light.one", room_id=None)
        self.engine.patch_user_device("device", gangs={
            "light.one": {"presentation": "solo", "type": "light"},
        })
        self.engine.set_gang_room("device", "light.one", None)
        with self.assertRaises(_REGISTRY.RoomMoveConflict):
            self.move({**self.payload, "gang_entity_id": "light.one",
                       "expected_rooms": {"light.one": None}, "expected_gang_override": False})

    def test_solo_ack_cannot_expose_other_room_members(self):
        self.engine.patch_user_device("device", gangs={
            "light.one": {"presentation": "solo", "type": "light"},
        })
        self.engine.assign_device("cover.two", room_id="b")
        with self.assertRaises(_REGISTRY.RoomMoveDenied):
            self.move({**self.payload, "gang_entity_id": "light.one", "room_id": "a",
                       "expected_rooms": {"light.one": "a"}, "expected_gang_override": False},
                      scope=["a"])

    def test_deleted_gang_room_becomes_unassigned_without_touching_parent(self):
        self.engine.patch_user_device("device", gangs={
            "light.one": {"presentation": "solo", "type": "light", "icon": "lamp"},
        })
        self.engine.set_gang_room("device", "light.one", "b")
        self.engine.delete_room("b")
        gang = self.engine.get_user_device("device")["gangs"]["light.one"]
        self.assertIsNone(gang["room_id"])
        self.assertTrue(gang["room_override"])
        self.assertEqual(gang["icon"], "lamp")
        self.assertEqual(self.engine.room_of("light.one"), "a")

    def test_receipts_are_bounded_and_expired_receipts_pruned(self):
        receipts = self.store.table("registry_room_moves")
        with self.store.transaction():
            receipts["expired"] = {"expires_at": 0}
            for index in range(1024):
                receipts[str(index)] = {"expires_at": 9999999999}
        self.move()
        self.assertNotIn("expired", receipts)
        self.assertEqual(len(receipts), 1024)

    def test_rejects_unknown_and_oversized_requests(self):
        for changes in ({"unknown": 1}, {"idempotency_key": "short"},
                        {"expected_rooms": {}}, {"expected_rooms": {str(i): None for i in range(101)}}):
            with self.subTest(changes=changes), self.assertRaises(_REGISTRY.RegistryError):
                self.move({**self.payload, **changes})
