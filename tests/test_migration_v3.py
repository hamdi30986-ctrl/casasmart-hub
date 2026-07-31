"""Migration v3: fold legacy flat gang_types/gang_names into nested gangs.

Run from the repo root:
    python3 -m unittest tests.test_migration_v3 -v
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

from storage.migrations import (  # noqa: E402
    MIGRATIONS,
    _migration_v3,
    _v3_suffix_token,
    get_user_version,
    run_migrations,
)


class MigrationV3Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "casasmart.db"
        self.backup_dir = self.dir / "backups"

    def _fold(self, records: dict) -> dict:
        """Seed a v2 db with [records], migrate to latest, return the result."""
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:2])
        with sqlite3.connect(self.db_path) as conn:
            for key, rec in records.items():
                conn.execute(
                    "INSERT INTO kv (namespace, key, value) "
                    "VALUES ('registry_user_devices', ?, ?)",
                    (key, json.dumps(rec)),
                )
            conn.commit()
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:3])
        out = {}
        with sqlite3.connect(self.db_path) as conn:
            for key, val in conn.execute(
                "SELECT key, value FROM kv WHERE namespace='registry_user_devices'"
            ).fetchall():
                out[key] = json.loads(val)
        return out

    # ── the three real dev-hub shapes (inconsistent legacy keying) ──

    def test_gang_n_keyed_single_entity(self):
        # outdoor: gang_types keyed by the positional 'gang_1' fallback.
        out = self._fold({
            "dev": {
                "entity_ids": ["switch.outdoor"],
                "gang_types": {"gang_1": "light"},
                "gang_names": {"gang_1": "Gang 1"},
            }
        })
        gangs = out["dev"]["gangs"]
        self.assertEqual(list(gangs), ["switch.outdoor"])
        self.assertEqual(gangs["switch.outdoor"]["type"], "light")
        self.assertEqual(gangs["switch.outdoor"]["presentation"], "grouped")

    def test_entity_id_keyed_single_entity(self):
        # dim_bedroom_ac_light: gang_types keyed by the full entity_id.
        out = self._fold({
            "dev": {
                "entity_ids": ["switch.dim_bedroom_ac_light"],
                "gang_types": {"switch.dim_bedroom_ac_light": "fan"},
                "gang_names": {},
            }
        })
        self.assertEqual(
            out["dev"]["gangs"]["switch.dim_bedroom_ac_light"]["type"], "fan"
        )

    def test_mismatched_key_single_entity(self):
        # stairs: entity is light.stairs (switch_as_x'd) but the gang_types key
        # is switch.stairs — the sole-value rule still types the one gang.
        out = self._fold({
            "dev": {
                "entity_ids": ["light.stairs"],
                "gang_types": {"switch.stairs": "light"},
                "gang_names": {},
            }
        })
        gangs = out["dev"]["gangs"]
        self.assertEqual(list(gangs), ["light.stairs"])
        self.assertEqual(gangs["light.stairs"]["type"], "light")

    # ── multi-gang (customer hubs): suffix-keyed maps ──

    def test_multigang_suffix_keyed(self):
        out = self._fold({
            "dev": {
                "entity_ids": ["switch.kitchen_left", "switch.kitchen_right"],
                "gang_types": {"left": "light", "right": "switch"},
                "gang_names": {"left": "Lounge"},
            }
        })
        gangs = out["dev"]["gangs"]
        self.assertEqual(gangs["switch.kitchen_left"], {
            "type": "light", "icon": None, "name": "Lounge",
            "presentation": "grouped",
        })
        self.assertEqual(gangs["switch.kitchen_right"], {
            "type": "switch", "icon": None, "name": None,
            "presentation": "grouped",
        })

    # ── non-relay devices keep NO gangs (rendered by their real domain) ──

    def test_empty_gang_types_stays_domain_rendered(self):
        out = self._fold({
            "climate.bedroom": {
                "entity_ids": ["climate.bedroom"],
                "gang_types": {},
                "gang_names": {},
            }
        })
        self.assertNotIn("gangs", out["climate.bedroom"])

    # ── validation + defaults ──

    def test_unknown_type_defaults_to_switch(self):
        out = self._fold({
            "dev": {
                "entity_ids": ["switch.gate"],
                "gang_types": {"gang_1": "cover"},  # not in the known set
                "gang_names": {},
            }
        })
        self.assertEqual(out["dev"]["gangs"]["switch.gate"]["type"], "switch")

    def test_heater_and_outlet_pass(self):
        out = self._fold({
            "h": {"entity_ids": ["switch.h"], "gang_types": {"switch.h": "heater"}},
            "o": {"entity_ids": ["switch.o"], "gang_types": {"switch.o": "outlet"}},
        })
        self.assertEqual(out["h"]["gangs"]["switch.h"]["type"], "heater")
        self.assertEqual(out["o"]["gangs"]["switch.o"]["type"], "outlet")

    # ── idempotency ──

    def test_already_nested_record_untouched(self):
        served = {
            "type": "fan", "icon": "x", "name": "Y", "presentation": "solo",
        }
        out = self._fold({
            "dev": {
                "entity_ids": ["switch.a"],
                "gang_types": {"switch.a": "light"},  # would fold to light/grouped
                "gangs": {"switch.a": served},  # but a nested map already exists
            }
        })
        self.assertEqual(out["dev"]["gangs"]["switch.a"], served)

    def test_rerun_is_a_noop(self):
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:2])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO kv (namespace, key, value) "
                "VALUES ('registry_user_devices', 'dev', ?)",
                (json.dumps({
                    "entity_ids": ["switch.a"],
                    "gang_types": {"switch.a": "light"},
                }),),
            )
            conn.commit()
            _migration_v3(conn)
            first = conn.execute(
                "SELECT value FROM kv WHERE key='dev'"
            ).fetchone()[0]
            _migration_v3(conn)  # second pass
            second = conn.execute(
                "SELECT value FROM kv WHERE key='dev'"
            ).fetchone()[0]
        self.assertEqual(first, second)

    # ── version + framework ──

    def test_migrates_to_v3(self):
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:3])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(get_user_version(conn), 3)
        self.assertEqual(MIGRATIONS[2].version, 3)

    def test_malformed_record_is_skipped_not_fatal(self):
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:2])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO kv (namespace, key, value) "
                "VALUES ('registry_user_devices', 'bad', 'not json')",
            )
            conn.commit()
        # Must not raise, and must still reach v3.
        run_migrations(self.db_path, self.backup_dir, MIGRATIONS[:3])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(get_user_version(conn), 3)

    def test_suffix_token(self):
        self.assertEqual(_v3_suffix_token("switch.kitchen_left"), "left")
        self.assertEqual(_v3_suffix_token("switch.0x54ef_l1"), "l1")
        self.assertEqual(_v3_suffix_token("light.x_endpoint_2"), "endpoint_2")
        self.assertIsNone(_v3_suffix_token("switch.outdoor"))


if __name__ == "__main__":
    unittest.main()
