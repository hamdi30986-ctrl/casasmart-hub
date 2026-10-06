"""Additive storage and recoverable backup checks on disposable databases."""

import importlib
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_suggestions import rule, storage, stores

migrations = importlib.import_module("phase4_fixture.storage.migrations")


class StorageCompatibilityTest(unittest.TestCase):
    def test_additive_state_backup_restore_and_unknown_execution_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hub.db"
            db = storage.HubStorage(path)
            db.open()
            self.addCleanup(db.close)
            db.table("registry_user_devices")["device"] = {"custom_icon": "curtains"}
            before = sqlite3.connect(path)
            version = before.execute("PRAGMA user_version").fetchone()[0]
            before.close()
            suggestions = stores.SuggestionStore(db)
            suggestions.replace_rules(0, [rule()])
            now = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
            occurrence = {
                "occurrence_id": "fixture",
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }
            self.assertTrue(suggestions.claim(occurrence, now)[0])
            # Online SQLite backup must include committed WAL content.
            backup = migrations.backup_database(path, Path(temp) / "backups")
            check = sqlite3.connect(backup)
            self.assertEqual(
                check.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                check.execute("PRAGMA user_version").fetchone()[0], version
            )
            check.close()
            db.close()
            restored = Path(temp) / "restored.db"
            migrations.restore_database(restored, backup)
            reopened = storage.HubStorage(restored)
            reopened.open()
            self.addCleanup(reopened.close)
            recovered = stores.SuggestionStore(reopened)
            recovered.recover()
            self.assertEqual(
                recovered.snapshot()["executions"]["fixture"]["status"], "unknown"
            )
            self.assertFalse(recovered.claim(occurrence, now)[0])
            self.assertEqual(
                reopened.table("registry_user_devices")["device"]["custom_icon"],
                "curtains",
            )
            self.assertEqual(recovered.snapshot()["revision"], 1)

    def test_future_document_fails_closed_without_rewriting_existing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            db = storage.HubStorage(Path(temp) / "hub.db")
            db.open()
            self.addCleanup(db.close)
            future = {"version": 99, "future_field": "preserve"}
            db.table("suggestions_v1")["state"] = future
            with self.assertRaises(Exception) as caught:
                stores.SuggestionStore(db).recover()
            self.assertEqual(caught.exception.code, "unsupported_suggestion_storage")
            self.assertEqual(db.table("suggestions_v1")["state"], future)
