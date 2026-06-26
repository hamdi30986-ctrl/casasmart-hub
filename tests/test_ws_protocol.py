"""Unit tests for the B1.5 WebSocket protocol layer (stdlib unittest).

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

from ws_protocol import (  # noqa: E402
    ProtocolError,
    Subscription,
    auth_token,
    frame_auth_failed,
    frame_auth_ok,
    frame_auth_required,
    frame_entity_removed,
    frame_error,
    frame_pong,
    frame_state_changed,
    frame_subscribed,
    parse_client_frame,
    subscribe_entity_ids,
)


class TestParseClientFrame(unittest.TestCase):
    def test_valid_types(self):
        for frame_type in ("auth", "subscribe", "ping"):
            self.assertEqual(parse_client_frame({"type": frame_type}), frame_type)

    def test_rejects_non_object(self):
        for bad in (None, "auth", 42, ["auth"], True):
            with self.assertRaises(ProtocolError):
                parse_client_frame(bad)

    def test_rejects_missing_or_bad_type(self):
        for bad in ({}, {"type": None}, {"type": 1}, {"type": ""}):
            with self.assertRaises(ProtocolError):
                parse_client_frame(bad)

    def test_rejects_unknown_type(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_client_frame({"type": "unsubscribe"})
        self.assertIn("unsubscribe", str(ctx.exception))


class TestAuthToken(unittest.TestCase):
    def test_extracts_token(self):
        self.assertEqual(auth_token({"type": "auth", "token": "abc"}), "abc")

    def test_rejects_missing_empty_or_non_string(self):
        for frame in (
            {"type": "auth"},
            {"type": "auth", "token": ""},
            {"type": "auth", "token": 123},
            {"type": "auth", "token": None},
        ):
            with self.assertRaises(ProtocolError):
                auth_token(frame)


class TestSubscribeEntityIds(unittest.TestCase):
    def test_omitted_or_null_means_all(self):
        self.assertIsNone(subscribe_entity_ids({"type": "subscribe"}))
        self.assertIsNone(subscribe_entity_ids({"type": "subscribe", "entity_ids": None}))

    def test_explicit_list(self):
        result = subscribe_entity_ids(
            {"type": "subscribe", "entity_ids": ["light.a", "switch.b"]}
        )
        self.assertEqual(result, frozenset({"light.a", "switch.b"}))

    def test_empty_list_is_valid_and_matches_nothing(self):
        result = subscribe_entity_ids({"type": "subscribe", "entity_ids": []})
        self.assertEqual(result, frozenset())

    def test_rejects_malformed(self):
        for bad in ("light.a", {"a": 1}, [1, 2], ["light.a", ""], [None]):
            with self.assertRaises(ProtocolError):
                subscribe_entity_ids({"type": "subscribe", "entity_ids": bad})


class TestSubscription(unittest.TestCase):
    def test_inactive_until_subscribed(self):
        sub = Subscription()
        self.assertFalse(sub.active)
        self.assertFalse(sub.matches("light.a"))

    def test_subscribe_all(self):
        sub = Subscription()
        sub.set(None)
        self.assertTrue(sub.active)
        self.assertTrue(sub.matches("light.a"))
        self.assertTrue(sub.matches("sensor.anything"))

    def test_subscribe_filtered(self):
        sub = Subscription()
        sub.set(frozenset({"light.a"}))
        self.assertTrue(sub.matches("light.a"))
        self.assertFalse(sub.matches("light.b"))

    def test_resubscribe_replaces(self):
        sub = Subscription()
        sub.set(frozenset({"light.a"}))
        sub.set(frozenset({"light.b"}))
        self.assertFalse(sub.matches("light.a"))
        self.assertTrue(sub.matches("light.b"))

    def test_subscribe_empty_set_matches_nothing(self):
        sub = Subscription()
        sub.set(frozenset())
        self.assertTrue(sub.active)
        self.assertFalse(sub.matches("light.a"))


class TestServerFrames(unittest.TestCase):
    def test_auth_ok(self):
        frame = frame_auth_ok("0.1.0", 1)
        self.assertEqual(
            frame, {"type": "auth_ok", "hub_version": "0.1.0", "api_version": 1}
        )

    def test_auth_failed_and_required(self):
        self.assertEqual(
            frame_auth_failed("bad"), {"type": "auth_failed", "reason": "bad"}
        )
        self.assertEqual(
            frame_auth_required(30), {"type": "auth_required", "grace_seconds": 30}
        )

    def test_subscribed_counts_devices(self):
        devices = [{"entity_id": "light.a"}, {"entity_id": "light.b"}]
        frame = frame_subscribed(devices)
        self.assertEqual(frame["type"], "subscribed")
        self.assertEqual(frame["count"], 2)
        self.assertEqual(frame["devices"], devices)

    def test_state_changed_pong_error(self):
        device = {"entity_id": "light.a", "state": "on"}
        self.assertEqual(
            frame_state_changed(device), {"type": "state_changed", "device": device}
        )
        self.assertEqual(frame_pong(), {"type": "pong"})
        self.assertEqual(frame_error("nope"), {"type": "error", "message": "nope"})

    def test_entity_removed_frame(self):
        self.assertEqual(
            frame_entity_removed("light.a"),
            {"type": "entity_removed", "entity_id": "light.a"},
        )


if __name__ == "__main__":
    unittest.main()
