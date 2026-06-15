"""Unit tests for B8: push-token store.

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
from push import PushTokenStore  # noqa: E402


class PushTokenTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.storage.close)
        self.store = PushTokenStore(self.storage.table("push_tokens"))


class RegisterTests(PushTokenTestCase):
    def test_register_stores_token(self):
        self.store.register("dev-1", "fcm-token-abc", "ios")
        record = self.store.get_token("dev-1")
        self.assertIsNotNone(record)
        self.assertEqual(record["fcm_token"], "fcm-token-abc")
        self.assertEqual(record["platform"], "ios")
        self.assertIn("updated_at", record)

    def test_register_upserts_on_same_device(self):
        self.store.register("dev-1", "old-token", "ios")
        self.store.register("dev-1", "new-token", "ios")
        record = self.store.get_token("dev-1")
        self.assertEqual(record["fcm_token"], "new-token")

    def test_register_android(self):
        self.store.register("dev-2", "fcm-android", "android")
        record = self.store.get_token("dev-2")
        self.assertEqual(record["platform"], "android")

    def test_register_rejects_invalid_platform(self):
        with self.assertRaises(ValueError):
            self.store.register("dev-1", "token", "windows")

    def test_register_rejects_empty_token(self):
        with self.assertRaises(ValueError):
            self.store.register("dev-1", "", "ios")

    def test_register_rejects_oversized_token(self):
        with self.assertRaises(ValueError):
            self.store.register("dev-1", "x" * 5000, "ios")


class UnregisterTests(PushTokenTestCase):
    def test_unregister_removes_token(self):
        self.store.register("dev-1", "token", "ios")
        result = self.store.unregister("dev-1")
        self.assertTrue(result)
        self.assertIsNone(self.store.get_token("dev-1"))

    def test_unregister_nonexistent_returns_false(self):
        result = self.store.unregister("dev-999")
        self.assertFalse(result)


class QueryTests(PushTokenTestCase):
    def test_get_all_tokens_empty(self):
        self.assertEqual(self.store.get_all_tokens(), {})

    def test_get_all_tokens_multiple(self):
        self.store.register("dev-1", "token-1", "ios")
        self.store.register("dev-2", "token-2", "android")
        all_tokens = self.store.get_all_tokens()
        self.assertEqual(len(all_tokens), 2)
        self.assertIn("dev-1", all_tokens)
        self.assertIn("dev-2", all_tokens)
        self.assertEqual(all_tokens["dev-1"]["fcm_token"], "token-1")
        self.assertEqual(all_tokens["dev-2"]["fcm_token"], "token-2")

    def test_get_token_nonexistent_returns_none(self):
        self.assertIsNone(self.store.get_token("dev-999"))


if __name__ == "__main__":
    unittest.main()
