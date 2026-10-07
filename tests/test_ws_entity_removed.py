"""WebSocket entity_removed frames stay within the exposed domains.

A removal frame names the entity, so a connection subscribed to everything
must not learn about entities the app can never see (people, device trackers,
scripts). Needs a real Home Assistant, like the other WebSocket suites.
"""

from __future__ import annotations

import os
import sys
import types
import unittest

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
    async def _frames_after_removing(self, entity_id: str) -> list[dict]:
        conn = wsmod.WsConnection(
            types.SimpleNamespace(), types.SimpleNamespace(closed=False), "test"
        )
        conn._claims = {"sub": "device", "role": "admin"}
        conn._subscription.set(None)
        conn._on_state_changed(
            types.SimpleNamespace(data={"entity_id": entity_id, "new_state": None})
        )
        return [await conn._send_queue.get() for _ in range(len(conn._send_queue))]

    async def test_removing_an_exposed_entity_is_pushed(self) -> None:
        self.assertEqual(
            await self._frames_after_removing("light.kitchen"),
            [{"type": "entity_removed", "entity_id": "light.kitchen"}],
        )

    async def test_removing_an_entity_outside_the_exposed_domains_is_not(
        self,
    ) -> None:
        for entity_id in ("person.alice", "device_tracker.alice_phone", "script.x"):
            with self.subTest(entity_id=entity_id):
                self.assertEqual(await self._frames_after_removing(entity_id), [])


if __name__ == "__main__":
    unittest.main()
