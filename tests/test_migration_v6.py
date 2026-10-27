"""Migration v6: level columns on the room activity log, upgraded from a lived-in v5.

The v5 file holds activity rows written the way the 2.3.x recorder wrote them,
then the v6 code opens it. Every v5 row must come through unchanged, with no
level; a fresh hub gets the columns from the start.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage, MigrationError
from storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    _migration_v5,
    _migration_v6,
    get_user_version,
    run_migrations,
)

V5_COLUMNS = [
    ("id", "INTEGER"),
    ("t", "INTEGER"),
    ("utc_offset_min", "INTEGER"),
    ("room_id", "TEXT"),
    ("entity_id", "TEXT"),
    ("action", "TEXT"),
    ("source", "TEXT"),
    ("member_id", "TEXT"),
]
LEVEL_COLUMNS = [
    ("brightness", "INTEGER"),
    ("color_temp_kelvin", "INTEGER"),
    ("percentage", "INTEGER"),
    ("hvac_mode", "TEXT"),
    ("temperature", "REAL"),
    ("fan_mode", "TEXT"),
]
# (t, utc_offset_min, room_id, entity_id, action, source, member_id) as a
# 2.3.x hub logged them.
V5_ROWS = [
    (1792713600, 180, "majlis", "light.majlis", "on", "app", "mem-a"),
    (1792713660, 180, "majlis", "climate.majlis_ac", "on", "wall_switch", None),
    (1792713720, 180, "majlis", "climate.majlis_ac", "set", "ha_user", None),
    (1792717200, 180, "kitchen", "switch.kitchen_left", "off", "automation", None),
]


def _columns(conn):
    return [
        (row[1], row[2])
        for row in conn.execute("PRAGMA table_info(room_activity_events)")
    ]


def _dump(db_path):
    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in tables
        }


class MigrationV6Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "hub.db"
        self.backup_dir = self.dir / "backups"

    def _lived_in_v5(self):
        """A v5 hub.db with kv data and activity rows; returns its dump."""
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open(MIGRATIONS[:5])
        storage.table("registry_rooms")["majlis"] = {"name": "المجلس", "sort_order": 0}
        storage.table("now_room_habits")["regular:household"] = {"v": 3, "layer": {}}
        with storage.transaction():
            for row in V5_ROWS:
                storage._execute_write(
                    "INSERT INTO room_activity_events (t, utc_offset_min, room_id, "
                    "entity_id, action, source, member_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
        self.assertEqual(storage.schema_version, 5)
        storage.close()
        return _dump(self.db_path)

    def test_v6_is_the_latest_step(self):
        self.assertEqual(MIGRATIONS[5].version, 6)
        self.assertEqual(MIGRATIONS[-1].version, 6)
        self.assertEqual(LATEST_VERSION, 6)

    def test_a_fresh_hub_gets_the_level_columns(self):
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        self.addCleanup(storage.close)
        self.assertEqual(storage.schema_version, 6)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(_columns(conn), V5_COLUMNS + LEVEL_COLUMNS)

    def test_v5_to_v6_keeps_every_row_without_a_level(self):
        before = self._lived_in_v5()

        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        self.addCleanup(storage.close)
        self.assertEqual(storage.schema_version, 6)
        events = storage.room_activity().events()
        self.assertEqual(
            [
                (
                    e["t"],
                    e["utc_offset_min"],
                    e["room_id"],
                    e["entity_id"],
                    e["action"],
                    e["source"],
                    e["member_id"],
                )
                for e in reversed(events)
            ],
            V5_ROWS,
        )
        self.assertEqual([e["level"] for e in events], [None] * len(V5_ROWS))
        storage.close()

        after = _dump(self.db_path)
        self.assertEqual(set(after), set(before))
        for table, rows in before.items():
            if table == "room_activity_events":
                # The same rows, each with six empty level columns.
                self.assertEqual(
                    after[table], [row + (None,) * len(LEVEL_COLUMNS) for row in rows]
                )
            else:
                self.assertEqual(after[table], rows, table)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(_columns(conn), V5_COLUMNS + LEVEL_COLUMNS)
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(room_activity_events)")
            }
        self.assertEqual(
            indexes,
            {
                "idx_room_activity_room_t",
                "idx_room_activity_member_t",
                "idx_room_activity_t",
            },
        )
        backups = list(self.backup_dir.glob("hub-v5-*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(_dump(backups[0]), before)

    def test_new_rows_carry_levels_next_to_the_old_ones(self):
        self._lived_in_v5()
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        self.addCleanup(storage.close)
        table = storage.room_activity()
        table.append_many(
            [
                {
                    "t": 1792720800,
                    "utc_offset_min": 180,
                    "room_id": "majlis",
                    "entity_id": "climate.majlis_ac",
                    "action": "on",
                    "source": "app",
                    "member_id": "mem-a",
                    "level": {
                        "hvac_mode": "cool",
                        "temperature": 23,
                        "fan_mode": "low",
                    },
                }
            ]
        )
        newest, *old = table.events()
        self.assertEqual(
            newest["level"],
            {"hvac_mode": "cool", "temperature": 23.0, "fan_mode": "low"},
        )
        self.assertEqual([e["level"] for e in old], [None] * len(V5_ROWS))

    def test_reopening_at_v6_is_a_no_op(self):
        self._lived_in_v5()
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open()
        storage.close()
        snapshot = _dump(self.db_path)
        backups = sorted(self.backup_dir.glob("*.db"))
        self.assertEqual(run_migrations(self.db_path, self.backup_dir), 6)
        self.assertEqual(_dump(self.db_path), snapshot)
        self.assertEqual(sorted(self.backup_dir.glob("*.db")), backups)

    def test_step_is_idempotent(self):
        with sqlite3.connect(":memory:") as conn:
            _migration_v5(conn)
            _migration_v6(conn)
            _migration_v6(conn)  # every column there: nothing to add
            self.assertEqual(_columns(conn), V5_COLUMNS + LEVEL_COLUMNS)
        # A file that got some columns already (a step cut short by hand) is
        # finished, and the run ends at v6 with every row kept.
        self._lived_in_v5()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "ALTER TABLE room_activity_events ADD COLUMN brightness INTEGER"
            )
            conn.commit()
            self.assertEqual(get_user_version(conn), 5)
        self.assertEqual(run_migrations(self.db_path, self.backup_dir), 6)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(_columns(conn), V5_COLUMNS + LEVEL_COLUMNS)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM room_activity_events").fetchone(),
                (len(V5_ROWS),),
            )

    def test_a_2_3_hub_refuses_a_v6_file(self):
        # Forward only: going back to 2.3.x means restoring the v5 backup.
        self._lived_in_v5()
        run_migrations(self.db_path, self.backup_dir)
        with self.assertRaisesRegex(MigrationError, "newer than this code"):
            run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:5])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(get_user_version(conn), 6)


if __name__ == "__main__":
    unittest.main()
