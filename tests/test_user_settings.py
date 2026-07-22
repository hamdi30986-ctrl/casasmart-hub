"""Unit tests for MB-2: the per-user settings engine.

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

from storage import HubStorage  # noqa: E402
from user_settings import SettingsError, UserSettingsEngine  # noqa: E402

TILE = {"type": "toggle", "entityId": "light.kitchen", "name": "Kitchen"}


class UserSettingsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.storage.close)
        self.engine = UserSettingsEngine(self.storage.table("user_settings"))


class UserSettingsTests(UserSettingsTestCase):
    def test_empty_doc_for_unknown_user(self):
        self.assertEqual(
            self.engine.get("dev-1"),
            {"display_name": None, "widget_tiles": None},
        )

    def test_partial_update_leaves_other_field(self):
        self.engine.update("dev-1", {"display_name": "  Alex  "})
        doc = self.engine.update("dev-1", {"widget_tiles": [TILE]})
        self.assertEqual(doc["display_name"], "Alex")  # trimmed, untouched
        self.assertEqual(doc["widget_tiles"], [TILE])

    def test_explicit_null_clears(self):
        self.engine.update("dev-1", {"display_name": "Alex", "widget_tiles": [TILE]})
        doc = self.engine.update("dev-1", {"display_name": None})
        self.assertIsNone(doc["display_name"])
        self.assertEqual(doc["widget_tiles"], [TILE])  # untouched

    def test_delete_drops_the_row(self):
        self.engine.update("dev-1", {"display_name": "Alex"})
        self.engine.delete("dev-1")
        self.assertEqual(
            self.engine.get("dev-1"),
            {"display_name": None, "widget_tiles": None},
        )
        self.engine.delete("dev-1")  # no-op when already absent

    def test_empty_string_clears_like_null(self):
        self.engine.update("dev-1", {"display_name": "Alex"})
        doc = self.engine.update("dev-1", {"display_name": "   "})
        self.assertIsNone(doc["display_name"])

    def test_fully_cleared_row_is_deleted(self):
        table = self.storage.table("user_settings")
        self.engine.update("dev-1", {"display_name": "Alex"})
        self.assertIn("dev-1", table)
        self.engine.update("dev-1", {"display_name": None})
        self.assertNotIn("dev-1", table)

    def test_users_are_isolated(self):
        self.engine.update("dev-1", {"display_name": "Alex"})
        self.engine.update("dev-2", {"display_name": "Mazin"})
        self.assertEqual(self.engine.get("dev-1")["display_name"], "Alex")
        self.assertEqual(self.engine.get("dev-2")["display_name"], "Mazin")

    def test_persists_across_reopen(self):
        self.engine.update("dev-1", {"display_name": "Alex", "widget_tiles": [TILE]})
        fresh = UserSettingsEngine(self.storage.table("user_settings"))
        doc = fresh.get("dev-1")
        self.assertEqual(doc["display_name"], "Alex")
        self.assertEqual(doc["widget_tiles"], [TILE])

    def test_tile_validation(self):
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {"widget_tiles": "nope"})
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {"widget_tiles": ["nope"]})
        with self.assertRaises(SettingsError):
            self.engine.update(
                "dev-1", {"widget_tiles": [{"type": "toggle", "name": "x"}]}
            )
        with self.assertRaises(SettingsError):
            self.engine.update(
                "dev-1",
                {"widget_tiles": [{**TILE, "entityId": ""}]},
            )
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {"widget_tiles": [TILE] * 65})
        # Unknown tile keys are dropped, not stored.
        doc = self.engine.update(
            "dev-1", {"widget_tiles": [{**TILE, "sneaky": "x"}]}
        )
        self.assertEqual(doc["widget_tiles"], [TILE])

    def test_name_validation(self):
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {"display_name": 42})
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {"display_name": "x" * 65})

    def test_unknown_fields_and_empty_body_rejected(self):
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {"theme": "dark"})
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", {})
        with self.assertRaises(SettingsError):
            self.engine.update("dev-1", "not a dict")


if __name__ == "__main__":
    unittest.main()
