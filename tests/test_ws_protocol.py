"""Unit tests for the B1.5 WebSocket protocol layer (stdlib unittest).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from ws_protocol import (  # noqa: E402
    CoalescingSendQueue,
    ProtocolError,
    Subscription,
    auth_token,
    coalesce_key,
    frame_alarm_changed,
    frame_audio_changed,
    frame_auth_failed,
    frame_auth_ok,
    frame_auth_required,
    frame_entity_removed,
    frame_error,
    frame_pong,
    frame_registry_changed,
    frame_state_changed,
    frame_subscribed,
    frame_tank_changed,
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


def _state(entity_id, value="on"):
    """A state_changed frame for [entity_id] (device dict is duck-typed)."""
    return frame_state_changed({"entity_id": entity_id, "state": value})


class TestCoalesceKey(unittest.TestCase):
    def test_state_changed_keys_by_entity(self):
        self.assertEqual(
            coalesce_key(_state("light.a")), ("state_changed", "light.a")
        )
        # Same entity, different state -> same key (newer supersedes older).
        self.assertEqual(
            coalesce_key(_state("light.a", "on")),
            coalesce_key(_state("light.a", "off")),
        )

    def test_nudges_key_by_identity(self):
        self.assertEqual(
            coalesce_key(frame_registry_changed("floors")),
            ("registry_changed", "floors"),
        )
        self.assertEqual(
            coalesce_key(frame_tank_changed("dev-1")), ("tank_changed", "dev-1")
        )
        self.assertEqual(coalesce_key(frame_alarm_changed()), ("alarm_changed",))
        self.assertEqual(coalesce_key(frame_audio_changed()), ("audio_changed",))
        self.assertEqual(
            coalesce_key(frame_entity_removed("light.z")),
            ("entity_removed", "light.z"),
        )

    def test_protocol_frames_are_never_droppable(self):
        for frame in (
            frame_auth_ok("1.0", 1),
            frame_auth_failed("nope"),
            frame_auth_required(30),
            frame_subscribed([]),
            frame_pong(),
            frame_error("bad"),
        ):
            self.assertIsNone(coalesce_key(frame), frame)

    def test_malformed_state_frame_keys_none_entity(self):
        # Defensive: a device without entity_id still yields a stable key.
        self.assertEqual(
            coalesce_key({"type": "state_changed", "device": {}}),
            ("state_changed", None),
        )
        self.assertEqual(
            coalesce_key({"type": "state_changed"}),
            ("state_changed", None),
        )


class TestCoalescingSendQueue(unittest.IsolatedAsyncioTestCase):
    async def _drain(self, q):
        out = []
        while len(q):
            out.append(await q.get())
        return out

    async def test_state_for_same_entity_coalesces_in_place(self):
        q = CoalescingSendQueue(maxsize=8)
        self.assertTrue(q.offer(_state("light.a", "on")))
        self.assertTrue(q.offer(_state("light.b", "on")))
        self.assertTrue(q.offer(_state("light.a", "off")))  # supersedes a=on
        frames = await self._drain(q)
        # Two frames (a, b), a carries the NEWEST state, order preserved.
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["device"]["entity_id"], "light.a")
        self.assertEqual(frames[0]["device"]["state"], "off")
        self.assertEqual(frames[1]["device"]["entity_id"], "light.b")

    async def test_redundant_nudges_collapse(self):
        q = CoalescingSendQueue(maxsize=8)
        for _ in range(5):
            self.assertTrue(q.offer(frame_alarm_changed()))
        self.assertTrue(q.offer(frame_registry_changed("rooms")))
        self.assertTrue(q.offer(frame_registry_changed("rooms")))
        frames = await self._drain(q)
        self.assertEqual(
            [f["type"] for f in frames], ["alarm_changed", "registry_changed"]
        )

    async def test_over_cap_drops_oldest_push_not_socket(self):
        q = CoalescingSendQueue(maxsize=3)
        for name in ("a", "b", "c"):  # fills the cap, all distinct entities
            self.assertTrue(q.offer(_state(f"light.{name}")))
        # Cap hit; a new distinct entity evicts the OLDEST push (light.a),
        # never returns False (socket stays open).
        self.assertTrue(q.offer(_state("light.d")))
        frames = await self._drain(q)
        ids = [f["device"]["entity_id"] for f in frames]
        self.assertEqual(ids, ["light.b", "light.c", "light.d"])

    async def test_protocol_frames_never_dropped_and_bypass_cap(self):
        q = CoalescingSendQueue(maxsize=2)
        q.put_protocol(frame_auth_ok("1.0", 1))
        q.put_protocol(frame_subscribed([]))
        # Cap is 2 and both slots hold protocol frames — a push can't evict
        # them, so offer() reports the consumer is hopeless (caller closes).
        self.assertFalse(q.offer(_state("light.a")))
        frames = await self._drain(q)
        self.assertEqual([f["type"] for f in frames], ["auth_ok", "subscribed"])

    async def test_push_evicts_only_droppable_when_protocol_present(self):
        q = CoalescingSendQueue(maxsize=3)
        q.put_protocol(frame_auth_ok("1.0", 1))  # undroppable, oldest
        self.assertTrue(q.offer(_state("light.a")))
        self.assertTrue(q.offer(_state("light.b")))  # cap full now
        # Over cap: must skip the protocol frame and drop the oldest PUSH (a).
        self.assertTrue(q.offer(_state("light.c")))
        frames = await self._drain(q)
        self.assertEqual(frames[0]["type"], "auth_ok")
        ids = [f["device"]["entity_id"] for f in frames[1:]]
        self.assertEqual(ids, ["light.b", "light.c"])

    async def test_get_waits_for_a_frame_then_returns_it(self):
        q = CoalescingSendQueue(maxsize=4)
        getter = asyncio.ensure_future(q.get())
        await asyncio.sleep(0)  # let the getter park on the empty queue
        self.assertFalse(getter.done())
        q.put_protocol(frame_pong())
        frame = await asyncio.wait_for(getter, timeout=1)
        self.assertEqual(frame["type"], "pong")


if __name__ == "__main__":
    unittest.main()
