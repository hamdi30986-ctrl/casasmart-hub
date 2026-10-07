"""WebSocket entity_removed frames name only entities the app was shown.

A removal frame names the entity, so a connection must not learn about
entities it could never see: another room's devices on a room-scoped
connection, or domains the app never shows (people, device trackers,
scripts). A connection is told of a removal when the entity was in its
snapshot or in a state change it was sent, even if the entity's area is
already gone. Needs a real Home Assistant, like the other WebSocket suites.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart import ws as wsmod

    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class EntityRemovedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = {"kitchen": {"light.kitchen"}}
        self.hass = types.SimpleNamespace(states=H.FakeStates())
        for entity_id in ("light.kitchen", "light.bedroom"):
            self.hass.states.add(entity_id)
        for name, value in (
            ("is_served", H.is_served_for(["light.kitchen", "light.bedroom"])),
            (
                "in_scope",
                lambda hass, eid, rooms: H.in_scope_for(self.scope)(hass, eid, rooms),
            ),
            ("serialize_device", lambda hass, state: {"entity_id": state.entity_id}),
        ):
            patcher = mock.patch.object(wsmod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _connect(self, rooms=None):
        conn = wsmod.WsConnection(
            self.hass, types.SimpleNamespace(closed=False), "test"
        )
        role = "admin" if rooms is None else "user"
        conn._claims = {"sub": "device", "role": role, "rooms": rooms}
        conn._subscription.set(None)
        return conn

    @staticmethod
    def _changed(conn, entity_id: str) -> None:
        new_state = types.SimpleNamespace(entity_id=entity_id)
        conn._on_state_changed(
            types.SimpleNamespace(data={"entity_id": entity_id, "new_state": new_state})
        )

    @staticmethod
    async def _removed(conn, entity_id: str) -> list[dict]:
        conn._send_queue.drop_data()
        conn._on_state_changed(
            types.SimpleNamespace(data={"entity_id": entity_id, "new_state": None})
        )
        return [await conn._send_queue.get() for _ in range(len(conn._send_queue))]

    async def test_removing_an_entity_the_app_was_sent_is_pushed(self) -> None:
        conn = self._connect()
        self._changed(conn, "light.kitchen")
        self.assertEqual(
            await self._removed(conn, "light.kitchen"),
            [{"type": "entity_removed", "entity_id": "light.kitchen"}],
        )

    async def test_an_entity_in_the_snapshot_counts_as_shown(self) -> None:
        conn = self._connect()
        await conn._emit_snapshot()
        self.assertEqual(
            await self._removed(conn, "light.bedroom"),
            [{"type": "entity_removed", "entity_id": "light.bedroom"}],
        )

    async def test_another_rooms_entity_is_never_named(self) -> None:
        conn = self._connect(rooms=["kitchen"])
        await conn._emit_snapshot()
        self._changed(conn, "light.bedroom")
        self.assertEqual(await self._removed(conn, "light.bedroom"), [])

    async def test_removal_reaches_the_app_after_its_area_is_gone(self) -> None:
        conn = self._connect(rooms=["kitchen"])
        self._changed(conn, "light.kitchen")
        self.scope = {}  # the kitchen area was deleted first
        self.assertEqual(
            await self._removed(conn, "light.kitchen"),
            [{"type": "entity_removed", "entity_id": "light.kitchen"}],
        )

    async def test_removing_an_entity_outside_the_exposed_domains_is_not(
        self,
    ) -> None:
        conn = self._connect()
        for entity_id in ("person.alice", "device_tracker.alice_phone", "script.x"):
            with self.subTest(entity_id=entity_id):
                self.assertEqual(await self._removed(conn, entity_id), [])


if __name__ == "__main__":
    unittest.main()
