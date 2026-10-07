"""WebSocket re-authentication: no data on a socket whose token died.

When a connection's token fails revalidation (unpaired, re-scoped, expired)
the hub sends ``auth_required`` and keeps the socket open for a grace window
so the app can re-auth in-band. Until it does, the hub must send nothing that
carries home data — no ``subscribed`` snapshot, ``state_changed``,
``entity_removed`` or nudge — and drop such frames already queued. The
auth/pong/error dialogue continues. A successful re-auth resumes the stream
under the NEW token's claims, starting with a fresh snapshot.

The connection is the real ``WsConnection`` over a real AuthEngine (temp
HubStorage); only the socket, ``hass`` and the entity filter are faked — the
question is which claims apply and when, not the filter itself
(``test_filtering`` pins that). Needs a real Home Assistant (``ws`` imports its
JSON helpers), like the other view-layer suites.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from aiohttp import WSMsgType
    from casasmart import ws as wsmod
    from casasmart.const import WS_CLOSE_AUTH_EXPIRED

    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR

# The frames that carry no home data — all a socket awaiting re-auth may get.
_CONTROL = {"auth_ok", "auth_failed", "auth_required", "pong", "error"}

_SCOPE_MAP = {
    "room-a": {"light.room_a"},
    "room-b": {"light.room_b", "lock.front_door"},
}


class FakeSocket:
    """The aiohttp ``WebSocketResponse`` surface ``WsConnection`` uses."""

    def __init__(self) -> None:
        self.closed = False
        self.close_code: int | None = None
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()

    def client_sends(self, frame: dict) -> None:
        self._incoming.put_nowait(
            types.SimpleNamespace(type=WSMsgType.TEXT, json=lambda: frame)
        )

    async def receive(self):
        return await self._incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._incoming.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def send_json(self, frame, dumps=None):
        self.sent.append(frame)

    async def close(self, code=None, message=None):
        self.closed = True
        self.close_code = code
        self._incoming.put_nowait(None)


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class WsReauthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        for entity_id in ("light.room_a", "light.room_b", "lock.front_door"):
            self.hass.states.add(entity_id, state="off")
        for name, value in (
            ("is_served", lambda hass, entity_id: True),
            ("in_scope", H.in_scope_for(_SCOPE_MAP)),
            (
                "serialize_device",
                lambda hass, state: {
                    "entity_id": state.entity_id,
                    "state": state.state,
                },
            ),
        ):
            patcher = mock.patch.object(wsmod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _connect(self, *, role="admin", rooms=None):
        """Auth + subscribe-all on a fresh connection; returns its pieces."""
        device_id = H.enroll(self.rt.auth, role=role, rooms=rooms)
        socket = FakeSocket()
        socket.client_sends(
            {
                "type": "auth",
                "token": H.token_for(self.rt.auth, device_id, role=role, rooms=rooms),
            }
        )
        conn = wsmod.WsConnection(self.hass, socket, "test")
        self.assertTrue(await conn._authenticate_first_frame())
        for coro in (conn._sender_loop(), conn._receive_loop()):
            task = asyncio.create_task(coro)
            self.addCleanup(task.cancel)
        self.addCleanup(conn.cleanup)
        socket.client_sends({"type": "subscribe"})
        await self._settle(conn)
        return device_id, socket, conn

    def _fresh_token(self, device_id: str) -> str:
        """What the app's re-login yields: a token for the device as it is NOW."""
        cached = self.rt.auth._device_cache[device_id]
        return H.auth_tokens.issue_token(
            self.rt.auth._signing_secret(),
            device_id,
            cached["role"],
            cached.get("rooms"),
            3600,
            ver=cached["ver"],
        )

    async def _settle(self, conn) -> None:
        """Let the receive loop and the single writer catch up."""
        for _ in range(20):
            await asyncio.sleep(0)
        while len(conn._send_queue):
            await asyncio.sleep(0)

    async def _detect(self, conn) -> None:
        """What the hub does after an admin edit: EVENT_AUTH_CHANGED → recheck."""
        conn._on_auth_changed(types.SimpleNamespace(data={}))
        await self.hass.created_tasks.pop()

    def _change_everything(self, conn, state: str) -> None:
        """Every data source a connection listens to fires once."""
        for entity_id in ("light.room_a", "light.room_b", "lock.front_door"):
            new_state = self.hass.states.add(entity_id, state=state)
            conn._on_state_changed(
                types.SimpleNamespace(
                    data={"entity_id": entity_id, "new_state": new_state}
                )
            )
        conn._on_state_changed(
            types.SimpleNamespace(data={"entity_id": "light.room_b", "new_state": None})
        )
        empty = types.SimpleNamespace(data={})
        conn._on_registry_changed(types.SimpleNamespace(data={"kind": "rooms"}))
        conn._on_tank_changed(types.SimpleNamespace(data={"device_id": "tank-1"}))
        conn._on_alarm_changed(empty)
        conn._on_audio_changed(empty)
        conn._on_energy_changed(empty)
        conn._on_suggestions_changed(empty)

    @staticmethod
    def _types(frames) -> list[str]:
        return [frame["type"] for frame in frames]

    def _data_frames(self, frames) -> list[dict]:
        return [frame for frame in frames if frame["type"] not in _CONTROL]

    async def test_unpaired_socket_sends_no_data_after_detection(self) -> None:
        # A sub-admin's socket is entitled to every frame kind (alarm, audio and
        # energy nudges included), and the admin can unpair it.
        device_id, socket, conn = await self._connect(role="sub-admin")
        # Sanity: while the token is good, every kind of frame flows.
        self._change_everything(conn, "on")
        await self._settle(conn)
        self.assertIn("state_changed", self._types(socket.sent))
        self.assertIn("alarm_changed", self._types(socket.sent))

        # Queued but not yet written when the revocation is detected: dropped.
        # (_detect runs the recheck inline, so the writer gets no turn first.)
        self._change_everything(conn, "off")
        self.rt.auth.delete_device(device_id)
        mark = len(socket.sent)
        await self._detect(conn)
        # During the grace window everything fires again, and the app (which
        # has not noticed yet) re-subscribes.
        self._change_everything(conn, "on")
        socket.client_sends({"type": "subscribe"})
        await self._settle(conn)

        after = socket.sent[mark:]
        self.assertEqual(self._data_frames(after), [])
        self.assertEqual(self._types(after), ["auth_required"])
        self.assertIsNone(conn._claims)

    async def test_periodic_recheck_suspends_data_too(self) -> None:
        # The 60 s recheck loop (no auth-changed event, e.g. an expired token)
        # takes the same path as the immediate recheck.
        device_id, socket, conn = await self._connect(role="user", rooms=["room-a"])
        self.rt.auth.delete_device(device_id)
        mark = len(socket.sent)
        with mock.patch.object(wsmod, "WS_TOKEN_RECHECK", 0):
            recheck = asyncio.create_task(conn._token_recheck_loop())
            self.addCleanup(recheck.cancel)
            await self._settle(conn)
            self._change_everything(conn, "off")
            socket.client_sends({"type": "subscribe"})
            await self._settle(conn)

        after = socket.sent[mark:]
        self.assertEqual(self._data_frames(after), [])
        self.assertEqual(self._types(after), ["auth_required"])

    async def test_grace_window_still_answers_ping_and_errors_then_closes(self) -> None:
        device_id, socket, conn = await self._connect(role="user")
        self.rt.auth.delete_device(device_id)
        mark = len(socket.sent)
        with mock.patch.object(wsmod, "WS_REAUTH_GRACE", 0.05):
            await self._detect(conn)
            socket.client_sends({"type": "ping"})
            socket.client_sends({"type": "bogus"})
            socket.client_sends({"type": "auth", "token": "not-a-token"})
            await self._settle(conn)
            self.assertEqual(
                self._types(socket.sent[mark:]),
                ["auth_required", "pong", "error", "auth_failed"],
            )
            await asyncio.sleep(0.1)
        self.assertTrue(socket.closed)
        self.assertEqual(socket.close_code, WS_CLOSE_AUTH_EXPIRED)

    async def test_narrowed_scope_resumes_with_the_new_rooms_only(self) -> None:
        device_id, socket, conn = await self._connect(
            role="user", rooms=["room-a", "room-b"]
        )
        self.rt.auth.update_device(device_id, rooms=["room-a"])
        mark = len(socket.sent)
        await self._detect(conn)
        self._change_everything(conn, "on")
        await self._settle(conn)
        self.assertEqual(self._types(socket.sent[mark:]), ["auth_required"])

        # The app logs in again and answers in-band with its fresh token.
        mark = len(socket.sent)
        socket.client_sends({"type": "auth", "token": self._fresh_token(device_id)})
        await self._settle(conn)
        resumed = socket.sent[mark:]
        self.assertEqual(self._types(resumed), ["auth_ok", "subscribed"])
        self.assertEqual(
            [device["entity_id"] for device in resumed[1]["devices"]],
            ["light.room_a"],
        )

        mark = len(socket.sent)
        self._change_everything(conn, "off")
        await self._settle(conn)
        pushed = [
            frame["device"]["entity_id"]
            for frame in socket.sent[mark:]
            if frame["type"] == "state_changed"
        ]
        self.assertEqual(pushed, ["light.room_a"])

    async def test_successful_reauth_resumes_data(self) -> None:
        device_id, socket, conn = await self._connect(role="sub-admin")
        # Any edit bumps ver: the old token is stale, not revoked.
        self.rt.auth.update_device(device_id, role="sub-admin")
        await self._detect(conn)
        await self._settle(conn)
        self.assertIsNone(conn._token)

        mark = len(socket.sent)
        socket.client_sends({"type": "auth", "token": self._fresh_token(device_id)})
        await self._settle(conn)
        self._change_everything(conn, "on")
        await self._settle(conn)

        sent = self._types(socket.sent[mark:])
        self.assertEqual(sent[:2], ["auth_ok", "subscribed"])
        for kind in (
            "state_changed",
            "entity_removed",
            "registry_changed",
            "tank_changed",
            "alarm_changed",
            "audio_changed",
            "energy_changed",
            "suggestions_changed",
        ):
            self.assertIn(kind, sent)


if __name__ == "__main__":
    unittest.main()
