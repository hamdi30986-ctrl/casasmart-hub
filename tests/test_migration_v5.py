"""Migration v5: the append-only room activity table, upgraded from a lived-in v4.

The v4 file is filled the way a running hub fills it (registry, auth, Now,
Energy Saving, tank and suggestion data, in kv and the two append-only tables),
then opened by the v5 code. Every v4 row must come through unchanged.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage
from storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    _migration_v5,
    get_user_version,
    run_migrations,
)

# kv rows a hub of 2.3.x keeps, in the engines' own shapes.
V4_KV = {
    "registry_floors": {"floor-g": {"name": "Ground", "sort_order": 0}},
    "registry_rooms": {
        "room-AbCdEfGhIjK": {
            "name": "المجلس",
            "floor_id": "floor-g",
            "icon": "sofa",
            "sort_order": 2,
        },
        "kitchen": {"name": "Kitchen", "floor_id": None, "icon": None, "sort_order": 0},
    },
    "registry_devices": {
        "light.majlis_ceiling": {
            "room_id": "room-AbCdEfGhIjK",
            "display_name": "سقف",
            "sort_order": 0,
        },
        "switch.kitchen_relay_left": {
            "room_id": "kitchen",
            "display_name": None,
            "sort_order": 1,
        },
    },
    "registry_user_devices": {
        "ha-dev-1": {
            "entity_ids": ["switch.kitchen_relay_left", "switch.kitchen_relay_right"],
            "gangs": {
                "switch.kitchen_relay_left": {
                    "type": "light",
                    "icon": None,
                    "name": "Spots",
                    "presentation": "grouped",
                }
            },
        }
    },
    "auth_devices": {
        "dev-q1w2e3r4t5y6": {
            "name": "Owner's phone",
            "role": "admin",
            "public_key": "-----BEGIN PUBLIC KEY-----\nMCow\n-----END PUBLIC KEY-----",
            "rooms": None,
            "ver": 3,
            "member_id": "mem-z9y8x7w6v5u4",
        }
    },
    "now_recents": {
        "mem-z9y8x7w6v5u4": {
            "items": [
                {
                    "entity_id": "light.majlis_ceiling",
                    "at": "2026-10-22T19:04:11.512000+00:00",
                }
            ]
        }
    },
    "now_room_policies": {
        "kitchen": {
            "participates": True,
            "eligible_entity_ids": ["switch.kitchen_relay_left"],
        }
    },
    "now_restore_sets": {
        "kitchen": {
            "entity_ids": ["switch.kitchen_relay_left"],
            "captured_at": "2026-10-22T21:00:00+00:00",
        }
    },
    "energy_state": {"current": {"active_level": None, "released": {}}},
    "tank_devices": {"tank-1": {"name": "Roof tank", "token_sha256": "ab" * 32}},
    "suggestions_v1": {"rules": {"rules": [], "suppressions": {}}},
}


def _dump(db_path: Path) -> dict[str, list[tuple]]:
    """Every row of every table, ordered, from a plain read of the file."""
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


class MigrationV5Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "hub.db"
        self.backup_dir = self.dir / "backups"

    def _lived_in_v4(self) -> dict[str, list[tuple]]:
        """A v4 hub.db with data in every table; returns its dump."""
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open(MIGRATIONS[:4])
        for namespace, rows in V4_KV.items():
            table = storage.table(namespace)
            for key, value in rows.items():
                table[key] = value
        readings = storage.tank_readings()
        for i in range(288):
            readings.append("tank-1", 1761100000 + i * 300, 11.5 + i / 1000)
        events = storage.energy_events()
        for i in range(50):
            events.append(
                t=1761100000 + i,
                kind="released",
                level="smart",
                entity_id="light.majlis_ceiling",
                room_id="room-AbCdEfGhIjK",
                data={"source": "external", "n": i},
            )
        self.assertEqual(storage.schema_version, 4)
        storage.close()
        return _dump(self.db_path)

    def test_v5_is_the_fifth_step(self):
        self.assertEqual(MIGRATIONS[4].version, 5)
        self.assertGreaterEqual(LATEST_VERSION, 5)

    def test_schema_columns_and_indexes(self):
        with sqlite3.connect(":memory:") as conn:
            _migration_v5(conn)
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(room_activity_events)")
            ]
            indexes = {
                row[1]: row[4]  # name -> partial
                for row in conn.execute("PRAGMA index_list(room_activity_events)")
            }
            member_index = [
                row[2]
                for row in conn.execute("PRAGMA index_info(idx_room_activity_member_t)")
            ]
        self.assertEqual(
            columns,
            [
                "id",
                "t",
                "utc_offset_min",
                "room_id",
                "entity_id",
                "action",
                "source",
                "member_id",
            ],
        )
        self.assertEqual(
            indexes,
            {
                "idx_room_activity_room_t": 0,
                "idx_room_activity_member_t": 1,
                "idx_room_activity_t": 0,
            },
        )
        self.assertEqual(member_index, ["member_id", "t"])

    def test_v4_to_v5_keeps_every_row_and_takes_a_backup(self):
        before = self._lived_in_v4()

        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open(MIGRATIONS[:5])
        self.assertEqual(storage.schema_version, 5)
        self.assertEqual(storage.room_activity().count(), 0)
        storage.close()

        after = _dump(self.db_path)
        self.assertEqual(set(after) - set(before), {"room_activity_events"})
        for table, rows in before.items():
            if table == "sqlite_sequence":
                continue
            self.assertEqual(after[table], rows, table)
        self.assertEqual(after["room_activity_events"], [])
        # Engines read the data back as written.
        reopened = HubStorage(self.db_path, backup_dir=self.backup_dir)
        reopened.open(MIGRATIONS[:5])
        self.addCleanup(reopened.close)
        for namespace, rows in V4_KV.items():
            self.assertEqual(dict(reopened.table(namespace).items()), rows)

        backups = list(self.backup_dir.glob("hub-v4-*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(get_user_version(conn), 4)
        self.assertEqual(_dump(backups[0]), before)

    def test_reopening_at_v5_is_a_no_op(self):
        self._lived_in_v4()
        storage = HubStorage(self.db_path, backup_dir=self.backup_dir)
        storage.open(MIGRATIONS[:5])
        storage._execute_write(
            "INSERT INTO room_activity_events (t, utc_offset_min, room_id, "
            "entity_id, action, source) VALUES (1761200000, 180, 'kitchen', "
            "'switch.kitchen_relay_left', 'on', 'wall_switch')"
        )
        storage.close()
        snapshot = _dump(self.db_path)
        backups = sorted(self.backup_dir.glob("*.db"))

        self.assertEqual(
            run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:5]), 5
        )
        self.assertEqual(_dump(self.db_path), snapshot)
        # No new backup: nothing ran.
        self.assertEqual(sorted(self.backup_dir.glob("*.db")), backups)

    def test_step_is_idempotent_on_a_file_that_already_has_the_table(self):
        self._lived_in_v4()
        with sqlite3.connect(self.db_path) as conn:
            _migration_v5(conn)
            conn.execute(
                "INSERT INTO room_activity_events (t, utc_offset_min, room_id, "
                "entity_id, action, source) VALUES (1, 0, 'kitchen', 'light.a', "
                "'on', 'app')"
            )
            conn.commit()
            self.assertEqual(get_user_version(conn), 4)

        self.assertEqual(
            run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:5]), 5
        )
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM room_activity_events").fetchone(),
                (1,),
            )
            _migration_v5(conn)  # and once more, directly

    def test_kv_payloads_are_untouched_text(self):
        before = self._lived_in_v4()
        run_migrations(self.db_path, self.backup_dir)
        after = _dump(self.db_path)
        # The kv value column is compared as stored text, so key order and
        # Arabic text survive byte for byte.
        self.assertEqual(after["kv"], before["kv"])
        values = {row[2] for row in after["kv"]}
        self.assertIn(json.dumps(V4_KV["registry_rooms"]["kitchen"]), values)


if __name__ == "__main__":
    unittest.main()
