"""Unit tests for the B1.1 storage layer (stdlib unittest, no dependencies).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Import the storage subpackage directly — the casasmart package __init__
# imports homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import (  # noqa: E402
    ConfigError,
    HubStorage,
    JsonConfigStore,
    LATEST_VERSION,
    Migration,
    MigrationError,
    StorageError,
)
from storage.migrations import (  # noqa: E402
    MIGRATIONS,
    get_user_version,
    run_migrations,
)


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "casasmart.db"
        self.backup_dir = self.dir / "backups"

    def tearDown(self):
        self._tmp.cleanup()

    def make_storage(self) -> HubStorage:
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        self.addCleanup(storage.close)
        return storage


class TestWalAndLifecycle(StorageTestCase):
    def test_wal_mode_enabled(self):
        storage = self.make_storage()
        mode = storage._connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_schema_at_latest_version(self):
        storage = self.make_storage()
        self.assertEqual(storage.schema_version, LATEST_VERSION)

    def test_access_before_open_raises(self):
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        with self.assertRaises(StorageError):
            storage.table("users")["x"]

    def test_close_is_idempotent_and_reopenable(self):
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        storage.table("users")["u1"] = {"name": "Hamdi"}
        storage.close()
        storage.close()  # no error
        storage.open()
        self.assertEqual(storage.table("users")["u1"], {"name": "Hamdi"})
        storage.close()


class TestKeyValueTable(StorageTestCase):
    def test_set_get_roundtrip(self):
        users = self.make_storage().table("users")
        users["u1"] = {"name": "Hamdi", "role": "admin", "rooms": [1, 2]}
        self.assertEqual(users["u1"], {"name": "Hamdi", "role": "admin", "rooms": [1, 2]})

    def test_unicode_value_survives(self):
        t = self.make_storage().table("users")
        t["u1"] = {"name": "محمد حمدي"}
        self.assertEqual(t["u1"]["name"], "محمد حمدي")

    def test_overwrite_updates_value(self):
        t = self.make_storage().table("users")
        t["u1"] = {"v": 1}
        t["u1"] = {"v": 2}
        self.assertEqual(t["u1"], {"v": 2})
        self.assertEqual(len(t), 1)

    def test_missing_key_raises_keyerror(self):
        t = self.make_storage().table("users")
        with self.assertRaises(KeyError):
            t["nope"]
        with self.assertRaises(KeyError):
            del t["nope"]

    def test_delete_and_contains(self):
        t = self.make_storage().table("users")
        t["u1"] = 1
        self.assertIn("u1", t)
        del t["u1"]
        self.assertNotIn("u1", t)

    def test_iteration_and_len(self):
        t = self.make_storage().table("favorites")
        for i in range(5):
            t[f"k{i}"] = i
        self.assertEqual(len(t), 5)
        self.assertEqual(sorted(t), [f"k{i}" for i in range(5)])
        self.assertEqual(dict(t.items())["k3"], 3)

    def test_items_is_a_consistent_snapshot_during_delete(self):
        t = self.make_storage().table("devices")
        t["a"] = {"state": "on"}
        t["b"] = {"state": "off"}

        snapshot = t.items()
        del t["b"]

        self.assertEqual(
            dict(snapshot),
            {"a": {"state": "on"}, "b": {"state": "off"}},
        )
        self.assertEqual(dict(t.items()), {"a": {"state": "on"}})

    def test_namespaces_are_isolated(self):
        storage = self.make_storage()
        storage.table("users")["shared_key"] = "from-users"
        storage.table("favorites")["shared_key"] = "from-favorites"
        self.assertEqual(storage.table("users")["shared_key"], "from-users")
        self.assertEqual(storage.table("favorites")["shared_key"], "from-favorites")
        storage.table("users").clear()
        self.assertEqual(len(storage.table("users")), 0)
        self.assertEqual(len(storage.table("favorites")), 1)

    def test_invalid_namespace_rejected(self):
        storage = self.make_storage()
        for bad in ("Users", "1abc", "a b", "", "x" * 65, "users; DROP"):
            with self.assertRaises(ValueError, msg=bad):
                storage.table(bad)

    def test_non_string_key_rejected(self):
        t = self.make_storage().table("users")
        for bad in (1, None, b"x", ""):
            with self.assertRaises(TypeError, msg=repr(bad)):
                t[bad] = "v"

    def test_contains_answers_false_for_invalid_keys(self):
        """Membership is a question, not a write: `None in table` must be
        False, never TypeError (it aborted the registry seed on every boot
        for rooms whose HA area had no floor)."""
        t = self.make_storage().table("users")
        t["u1"] = 1
        for bad in (None, 1, b"x", ""):
            self.assertNotIn(bad, t, msg=repr(bad))
        self.assertIn("u1", t)

    def test_non_json_value_rejected(self):
        t = self.make_storage().table("users")
        with self.assertRaises(TypeError):
            t["k"] = object()
        self.assertNotIn("k", t)  # failed write left nothing behind

    def test_updated_at_returns_timestamp(self):
        t = self.make_storage().table("users")
        t["u1"] = 1
        stamp = t.updated_at("u1")
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_persistence_across_reopen(self):
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        storage.table("scenes")["movie_night"] = {"lights": "off"}
        storage.close()

        storage2 = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage2.open()
        self.addCleanup(storage2.close)
        self.assertEqual(storage2.table("scenes")["movie_night"], {"lights": "off"})


class TestMigrations(StorageTestCase):
    def test_fresh_db_migrates_to_latest_with_backup(self):
        self.make_storage()
        self.assertTrue(self.backup_dir.exists())
        backups = list(self.backup_dir.glob("*.db"))
        self.assertEqual(len(backups), 1)
        self.assertIn("-v0-", backups[0].name)

    def test_reopen_does_not_rerun_migrations(self):
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        storage.close()
        storage.open()
        storage.close()
        # only the first open migrated, so exactly one backup
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), 1)

    def test_v2_backfills_tank_readings_from_v1_blob(self):
        # Stand the db up at v1 only, then seed a legacy tank-readings blob.
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:1])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?)",
                (
                    "tank_readings",
                    "dev-1",
                    json.dumps(
                        {"entries": [{"t": 100, "v": 1.5}, {"t": 200, "v": 1.7}]}
                    ),
                ),
            )
            conn.commit()
        # Migrate to latest: v2 backfills the rows and drops the old blob.
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT t, v FROM tank_readings WHERE device_id='dev-1' "
                "ORDER BY t"
            ).fetchall()
            self.assertEqual(rows, [(100, 1.5), (200, 1.7)])
            leftover = conn.execute(
                "SELECT COUNT(*) FROM kv WHERE namespace='tank_readings'"
            ).fetchone()[0]
            self.assertEqual(leftover, 0)

    def test_failed_migration_restores_database(self):
        # v1 schema with data in it
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        storage.table("users")["u1"] = {"name": "Hamdi"}
        storage.close()

        def bad_migration(conn):
            conn.execute("CREATE TABLE half_done (x)")
            raise RuntimeError("boom mid-migration")

        broken = MIGRATIONS + (
            Migration(LATEST_VERSION + 1, "intentionally broken", bad_migration),
        )
        storage2 = HubStorage(self.db_path, backup_dir=self.backup_dir)
        with self.assertRaises(MigrationError):
            storage2.open(migrations=broken)

        # database restored: still at the latest shipped version, data intact,
        # no half-applied table
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(get_user_version(conn), LATEST_VERSION)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("half_done", tables)
            value = conn.execute(
                "SELECT value FROM kv WHERE namespace='users' AND key='u1'"
            ).fetchone()[0]
            self.assertEqual(json.loads(value), {"name": "Hamdi"})

    def test_downgrade_refused(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 99}")
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        with self.assertRaises(MigrationError):
            storage.open()

    def test_multi_step_migration_applies_in_order(self):
        order = []
        extra = MIGRATIONS + (
            Migration(2, "step two", lambda c: order.append(2)),
            Migration(3, "step three", lambda c: order.append(3)),
        )
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open(migrations=extra)
        self.addCleanup(storage.close)
        self.assertEqual(order, [2, 3])
        self.assertEqual(storage.schema_version, 3)


class TestJsonConfigStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "hub_config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_starts_empty(self):
        store = JsonConfigStore(self.path)
        self.assertEqual(store.as_dict(), {})
        self.assertIsNone(store.get("hub_id"))
        self.assertEqual(store.get("hub_id", "fallback"), "fallback")

    def test_set_persists_to_disk(self):
        JsonConfigStore(self.path).set("hub_id", "hub-001")
        self.assertEqual(JsonConfigStore(self.path).get("hub_id"), "hub-001")
        # file is real, well-formed JSON
        self.assertEqual(json.loads(self.path.read_text())["hub_id"], "hub-001")

    def test_update_batches_keys(self):
        store = JsonConfigStore(self.path)
        store.update({"a": 1, "b": [1, 2], "c": {"nested": True}})
        reloaded = JsonConfigStore(self.path)
        self.assertEqual(reloaded.as_dict(), {"a": 1, "b": [1, 2], "c": {"nested": True}})

    def test_delete_removes_key(self):
        store = JsonConfigStore(self.path)
        store.set("a", 1)
        store.delete("a")
        store.delete("a")  # deleting twice is a no-op, not an error
        self.assertIsNone(JsonConfigStore(self.path).get("a"))

    def test_corrupted_file_raises_config_error(self):
        self.path.write_text("{not json!!", encoding="utf-8")
        with self.assertRaises(ConfigError):
            JsonConfigStore(self.path)

    def test_non_object_json_rejected(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(ConfigError):
            JsonConfigStore(self.path)

    def test_non_json_value_rejected(self):
        store = JsonConfigStore(self.path)
        with self.assertRaises(ConfigError):
            store.set("bad", object())

    def test_as_dict_is_a_copy(self):
        store = JsonConfigStore(self.path)
        store.set("a", 1)
        snapshot = store.as_dict()
        snapshot["a"] = 999
        self.assertEqual(store.get("a"), 1)

    def test_no_temp_files_left_behind(self):
        store = JsonConfigStore(self.path)
        for i in range(5):
            store.set(f"k{i}", i)
        leftovers = [p for p in self.path.parent.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
