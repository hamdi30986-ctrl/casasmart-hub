"""Unit tests for camera stream tickets + HLS path validation (B16 3c-3).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from camera_streams import (  # noqa: E402
    MAX_TICKETS,
    TICKET_TTL,
    StreamTicketStore,
    TicketError,
    is_valid_hls_filename,
)

NOW = 1_000_000.0
CAM = "camera.front_door"


class TestHlsFilenameGate(unittest.TestCase):
    def test_real_ha_hls_artifacts_pass(self):
        for name in (
            "master_playlist.m3u8",
            "playlist.m3u8",
            "init.mp4",
            "segment/42.m4s",
            "segment/42.3.m4s",
        ):
            self.assertTrue(is_valid_hls_filename(name), name)

    def test_traversal_and_junk_rejected(self):
        for name in (
            "../secrets.yaml",
            "segment/../../configuration.yaml",
            "/etc/passwd",
            "a/b/c.m4s",
            "http://evil/x.m3u8",
            "playlist.m3u8 ",
            "",
            "noextension",
            "seg ment/1.m4s",
        ):
            self.assertFalse(is_valid_hls_filename(name), name)


class TestTicketStore(unittest.TestCase):
    def setUp(self):
        self.store = StreamTicketStore()

    def test_mint_then_validate(self):
        ticket = self.store.mint(CAM, now=NOW)
        # No raise = valid.
        self.store.validate(ticket.ticket_id, CAM, now=NOW + 1)

    def test_unknown_ticket_rejected(self):
        with self.assertRaises(TicketError):
            self.store.validate("nope", CAM, now=NOW)

    def test_wrong_entity_same_rejection_as_unknown(self):
        ticket = self.store.mint(CAM, now=NOW)
        with self.assertRaises(TicketError) as wrong:
            self.store.validate(ticket.ticket_id, "camera.other", now=NOW)
        with self.assertRaises(TicketError) as unknown:
            self.store.validate("nope", "camera.other", now=NOW)
        # Indistinguishable on purpose — nothing to enumerate.
        self.assertEqual(str(wrong.exception), str(unknown.exception))

    def test_expired_ticket_rejected_and_evicted(self):
        ticket = self.store.mint(CAM, now=NOW)
        with self.assertRaises(TicketError):
            self.store.validate(ticket.ticket_id, CAM, now=NOW + TICKET_TTL)
        self.assertEqual(len(self.store), 0)

    def test_ticket_valid_until_the_last_second(self):
        ticket = self.store.mint(CAM, now=NOW)
        self.store.validate(ticket.ticket_id, CAM, now=NOW + TICKET_TTL - 1)

    def test_mint_purges_expired(self):
        self.store.mint(CAM, now=NOW)
        self.store.mint(CAM, now=NOW + TICKET_TTL + 1)
        self.assertEqual(len(self.store), 1)

    def test_cap_evicts_soonest_to_expire(self):
        first = self.store.mint(CAM, now=NOW)
        for i in range(MAX_TICKETS - 1):
            self.store.mint(CAM, now=NOW + 1 + i * 0.001)
        self.assertEqual(len(self.store), MAX_TICKETS)
        newest = self.store.mint(CAM, now=NOW + 2)
        self.assertEqual(len(self.store), MAX_TICKETS)
        with self.assertRaises(TicketError):
            self.store.validate(first.ticket_id, CAM, now=NOW + 3)
        self.store.validate(newest.ticket_id, CAM, now=NOW + 3)

    def test_ticket_ids_unique_and_opaque(self):
        a = self.store.mint(CAM, now=NOW)
        b = self.store.mint(CAM, now=NOW)
        self.assertNotEqual(a.ticket_id, b.ticket_id)
        self.assertGreaterEqual(len(a.ticket_id), 24)


if __name__ == "__main__":
    unittest.main()
