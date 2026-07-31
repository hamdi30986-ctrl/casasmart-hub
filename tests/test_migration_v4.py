"""Migration v4: append-only Energy Saving event storage."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage  # noqa: E402
from storage.migrations import (  # noqa: E402
    LATEST_VERSION,
    MIGRATIONS,
    _migration_v4,
    get_user_version,
    run_migrations,
)


class MigrationV4Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "casasmart.db"
        self.backup_dir = self.dir / "backups"

    def test_v4_is_the_latest_append_only_step(self):
        self.assertEqual(MIGRATIONS[-1].version, 4)
        self.assertEqual(LATEST_VERSION, 4)

    def test_schema_has_event_columns_and_query_indexes(self):
        with sqlite3.connect(":memory:") as conn:
            _migration_v4(conn)
            columns = [
                row[1] for row in conn.execute("PRAGMA table_info(energy_events)")
            ]
            indexes = {
                row[1] for row in conn.execute("PRAGMA index_list(energy_events)")
            }
        self.assertEqual(
            columns,
            ["id", "t", "kind", "level", "entity_id", "room_id", "data"],
        )
        self.assertIn("idx_energy_events_t", indexes)
        self.assertIn("idx_energy_events_kind_t", indexes)

    def test_v3_to_v4_preserves_existing_kv_data(self):
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:3])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?)",
                ("users", "admin", json.dumps({"name": "محمد"})),
            )
            conn.commit()

        run_migrations(self.db_path, self.backup_dir, MIGRATIONS)

        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(get_user_version(conn), 4)
            raw = conn.execute(
                "SELECT value FROM kv WHERE namespace='users' AND key='admin'"
            ).fetchone()[0]
            self.assertEqual(json.loads(raw), {"name": "محمد"})
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM energy_events").fetchone()[0],
                0,
            )
        backups = list(self.backup_dir.glob("*-v3-*.db"))
        self.assertEqual(len(backups), 1)

    def test_event_rows_survive_wal_reopen(self):
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        event = storage.energy_events().append(
            t=1234,
            kind="activated",
            level="smart",
            room_id="living-room",
            data={"actor": "مسؤول"},
        )
        self.assertEqual(event["id"], 1)
        storage.close()

        reopened = HubStorage(self.db_path, backup_dir=self.backup_dir)
        reopened.open()
        self.addCleanup(reopened.close)
        self.assertEqual(
            reopened.energy_events().recent(),
            [
                {
                    "id": 1,
                    "t": 1234,
                    "kind": "activated",
                    "level": "smart",
                    "entity_id": None,
                    "room_id": "living-room",
                    "data": {"actor": "مسؤول"},
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
