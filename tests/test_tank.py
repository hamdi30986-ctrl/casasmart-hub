"""Unit tests for MB-1: the Shelly tank engine + script builder.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage  # noqa: E402
from tank import (  # noqa: E402
    TANK_SCRIPT_NAME,
    TankEngine,
    TankError,
    UnknownTankError,
    UnknownTokenError,
    build_tank_script,
    chunk_script_code,
)


class TankTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = HubStorage(Path(self._tmp.name) / "test.db")
        self.storage.open()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.storage.close)
        self.engine = TankEngine(
            self.storage.table("tank_devices"),
            self.storage.table("tank_readings"),
        )


class DeviceTests(TankTestCase):
    def test_mint_and_list(self):
        record, token = self.engine.mint_device(
            "ShellyPlusUni-AABB", "  Roof Tank  ", "192.168.8.59", "SNSN-0043X"
        )
        self.assertEqual(record["device_id"], "shellyplusuni-aabb")
        self.assertEqual(record["name"], "Roof Tank")
        self.assertEqual(record["ip"], "192.168.8.59")
        self.assertEqual(record["model"], "SNSN-0043X")
        self.assertIsNone(record["last_reading"])
        self.assertEqual(len(token), 32)  # token_hex(16)
        # The hash never leaves the engine.
        self.assertNotIn("token_sha256", record)
        listed = self.engine.list_devices()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token_sha256", listed[0])

    def test_reprovision_keeps_created_at_and_rotates_token(self):
        record1, token1 = self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        record2, token2 = self.engine.mint_device("dev-1", "Tank", "10.0.0.9")
        self.assertEqual(record1["created_at"], record2["created_at"])
        self.assertNotEqual(token1, token2)
        self.assertEqual(record2["ip"], "10.0.0.9")
        # Old token is dead, new one works.
        with self.assertRaises(UnknownTokenError):
            self.engine.ingest(token1, 1.5)
        self.assertEqual(self.engine.ingest(token2, 1.5), "dev-1")

    def test_validation(self):
        with self.assertRaises(TankError):
            self.engine.mint_device("", "Tank", "10.0.0.5")
        with self.assertRaises(TankError):
            self.engine.mint_device("dev-1", "", "10.0.0.5")
        with self.assertRaises(TankError):
            self.engine.mint_device("dev-1", "x" * 65, "10.0.0.5")
        with self.assertRaises(TankError):
            self.engine.mint_device("dev-1", "Tank", "")

    def test_delete_drops_readings_and_token(self):
        _, token = self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        self.engine.ingest(token, 1.5)
        self.engine.delete_device("dev-1")
        self.assertEqual(self.engine.list_devices(), [])
        with self.assertRaises(UnknownTokenError):
            self.engine.ingest(token, 1.6)
        with self.assertRaises(UnknownTankError):
            self.engine.delete_device("dev-1")
        with self.assertRaises(UnknownTankError):
            self.engine.recent_readings("dev-1")


class IngestTests(TankTestCase):
    def test_ingest_and_history(self):
        _, token = self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        self.engine.ingest(token, 1.5)
        self.engine.ingest(token, 1.7)
        readings = self.engine.recent_readings("dev-1", days=7)
        # Newest first — the app's fetchRecentReadings contract.
        self.assertEqual([entry["v"] for entry in readings], [1.7, 1.5])
        last = self.engine.last_reading("dev-1")
        self.assertEqual(last["v"], 1.7)
        # And the device list carries it.
        self.assertEqual(
            self.engine.list_devices()[0]["last_reading"]["v"], 1.7
        )

    def test_ingest_survives_reopen(self):
        _, token = self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        self.engine.ingest(token, 1.5)
        fresh = TankEngine(
            self.storage.table("tank_devices"),
            self.storage.table("tank_readings"),
        )
        self.assertEqual(fresh.ingest(token, 1.6), "dev-1")
        self.assertEqual(len(fresh.recent_readings("dev-1")), 2)

    def test_bad_tokens_and_values(self):
        self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        with self.assertRaises(UnknownTokenError):
            self.engine.ingest("deadbeef" * 4, 1.5)
        with self.assertRaises(UnknownTokenError):
            self.engine.ingest("", 1.5)
        with self.assertRaises(UnknownTokenError):
            self.engine.ingest(None, 1.5)
        _, token = self.engine.mint_device("dev-2", "Tank2", "10.0.0.6")
        with self.assertRaises(TankError):
            self.engine.ingest(token, "1.5")
        with self.assertRaises(TankError):
            self.engine.ingest(token, True)  # bool is not a voltage

    def test_days_filter(self):
        _, token = self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        self.engine.ingest(token, 1.5)
        # Backdate the entry beyond a 1-day window, keep it inside retention.
        table = self.storage.table("tank_readings")
        blob = table["dev-1"]
        blob["entries"][0]["t"] = int(time.time()) - 2 * 24 * 3600
        table["dev-1"] = blob
        self.assertEqual(self.engine.recent_readings("dev-1", days=1), [])
        self.assertEqual(len(self.engine.recent_readings("dev-1", days=7)), 1)
        with self.assertRaises(TankError):
            self.engine.recent_readings("dev-1", days=0)
        with self.assertRaises(TankError):
            self.engine.recent_readings("dev-1", days="7")

    def test_prune_caps_and_ages_out(self):
        _, token = self.engine.mint_device("dev-1", "Tank", "10.0.0.5")
        table = self.storage.table("tank_readings")
        now = int(time.time())
        # 9100 in-retention entries + 5 ancient ones.
        entries = [{"t": now - 40 * 24 * 3600, "v": 0.1} for _ in range(5)]
        entries += [{"t": now - index, "v": 1.0} for index in range(9100, 0, -1)]
        table["dev-1"] = {"entries": entries}
        self.engine.ingest(token, 2.0)
        stored = table["dev-1"]["entries"]
        self.assertEqual(len(stored), 9000)  # cap held
        self.assertEqual(stored[-1]["v"], 2.0)  # newest survived
        self.assertTrue(all(e["t"] >= now - 32 * 24 * 3600 for e in stored))


class ScriptBuilderTests(unittest.TestCase):
    def test_bakes_url_and_token(self):
        script = build_tank_script(
            "http://192.168.8.10:8123/api/casasmart/tank/reading", "aa" * 16
        )
        self.assertIn('"http://192.168.8.10:8123/api/casasmart/tank/reading"', script)
        self.assertIn(f'"{"aa" * 16}"', script)
        self.assertIn("Voltmeter", script)
        self.assertIn("device_token", script)
        self.assertIn("sec:300", script)
        # No template placeholders survive into a provisioned script.
        self.assertNotIn("YOUR_", script)

    def test_quote_injection_cannot_escape_literal(self):
        script = build_tank_script(
            'http://10.0.0.1/x"};evil();//', "tok"
        )
        # json.dumps escaped the quote — the literal is intact.
        self.assertIn('\\"};evil();//', script)

    def test_rejects_bad_inputs(self):
        with self.assertRaises(TankError):
            build_tank_script("ftp://10.0.0.1/x", "tok")
        with self.assertRaises(TankError):
            build_tank_script("http://10.0.0.1/x", "")
        with self.assertRaises(TankError):
            build_tank_script("http://10.0.0.1/x", "tok", interval_seconds=5)

    def test_chunking_round_trips(self):
        script = build_tank_script("http://10.0.0.1/reading", "ab" * 16)
        chunks = chunk_script_code(script, chunk_size=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode()) <= 100 for c in chunks))
        self.assertEqual("".join(chunks), script)

    def test_chunking_never_splits_multibyte(self):
        code = "x" * 99 + "é" + "y" * 50  # é straddles the 100-byte line
        chunks = chunk_script_code(code, chunk_size=100)
        self.assertEqual("".join(chunks), code)
        for chunk in chunks:
            chunk.encode("utf-8")  # every chunk is valid text on its own

    def test_script_name_constant(self):
        # The app's legacy cleanup path removes scripts by this exact name;
        # renaming it would orphan Supabase-era scripts on devices.
        self.assertEqual(TANK_SCRIPT_NAME, "CasaSmart")


if __name__ == "__main__":
    unittest.main()
