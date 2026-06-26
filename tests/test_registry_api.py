"""View-layer tests for ``registry_api`` — the favorites wire contract.

Pins the Phase 6/7 logic that lives in the VIEW, not the engine:
* GET filters phantom (gone/unserved) favorites, self-heals storage, scopes.
* PUT validates served+in-scope, is member-keyed (Phase 5), preserves a scoped
  caller's out-of-scope favorites (Phase 7), and fires the re-pull nudge.

Container/CI only (imports Home Assistant). Run:
    docker exec homeassistant-dev python3 -m unittest tests.test_registry_api -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H  # noqa: E402

try:
    from casasmart.registry_api import CasaSmartFavoritesView
    from casasmart.const import EVENT_REGISTRY_CHANGED

    _ERR = None
except Exception as err:  # noqa: BLE001 — HA-free local env
    CasaSmartFavoritesView = EVENT_REGISTRY_CHANGED = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class FavoritesViewTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.view = CasaSmartFavoritesView(self.hass)

    # -- helpers ---------------------------------------------------------------
    def _serve(self, *entity_ids):
        """Mark entity_ids as existing (in states) AND served."""
        for eid in entity_ids:
            self.hass.states.add(eid)
        return mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(entity_ids),
            in_scope=H.in_scope_for(),
        )

    async def _get(self, headers):
        resp = await self.view.get(H.FakeRequest(headers=headers))
        return H.read_response(resp)

    async def _put(self, headers, entity_ids):
        resp = await self.view.put(
            H.FakeRequest(headers=headers, body={"entity_ids": entity_ids})
        )
        return H.read_response(resp)


class FavoritesGet(FavoritesViewTestCase):
    async def test_returns_member_favorites(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(_unpack(hdr, self.rt))
        self.rt.registry.set_favorites(member, ["light.a", "light.b"])
        with self._serve("light.a", "light.b"):
            status, body = await self._get(hdr)
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_ids"], ["light.a", "light.b"])

    async def test_no_token_is_401(self) -> None:
        status, _ = await self._get({})
        self.assertEqual(status, 401)

    async def test_drops_phantom_and_self_heals_storage(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        # light.a is real+served; switch.gone was deleted (no state at all).
        self.rt.registry.set_favorites(member, ["light.a", "switch.gone"])
        self.hass.states.add("light.a")
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a", "switch.gone"]),
            in_scope=H.in_scope_for(),
        ):
            status, body = await self._get(hdr)
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_ids"], ["light.a"])  # phantom dropped
        # SELF-HEAL: the stored list itself lost the phantom.
        self.assertEqual(self.rt.registry.get_favorites(member), ["light.a"])

    async def test_drops_unserved_but_keeps_existing(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        self.rt.registry.set_favorites(member, ["light.a", "switch.hidden"])
        self.hass.states.add("light.a")
        self.hass.states.add("switch.hidden")  # exists but is_served=False
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a"]),  # hidden not served
            in_scope=H.in_scope_for(),
        ):
            status, body = await self._get(hdr)
        self.assertEqual(body["entity_ids"], ["light.a"])

    async def test_scopes_to_caller_without_healing_out_of_scope(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        member = self.rt.auth.member_id_for(dev)
        self.rt.registry.set_favorites(member, ["light.a", "light.b"])
        self.hass.states.add("light.a")
        self.hass.states.add("light.b")
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a", "light.b"]),
            in_scope=H.in_scope_for({"room1": {"light.a"}}),  # b is elsewhere
        ):
            status, body = await self._get(hdr)
        self.assertEqual(body["entity_ids"], ["light.a"])  # scoped view
        # Out-of-scope b is alive — must NOT be healed away.
        self.assertEqual(
            self.rt.registry.get_favorites(member), ["light.a", "light.b"]
        )


class FavoritesPut(FavoritesViewTestCase):
    async def test_replaces_for_admin(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        with self._serve("light.a", "light.b"):
            status, body = await self._put(hdr, ["light.a", "light.b"])
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_ids"], ["light.a", "light.b"])
        self.assertEqual(
            self.rt.registry.get_favorites(member), ["light.a", "light.b"]
        )

    async def test_rejects_unserved_entity(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        self.hass.states.add("light.a")
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a"]),  # bogus not served
            in_scope=H.in_scope_for(),
        ):
            status, _ = await self._put(hdr, ["light.a", "switch.bogus"])
        self.assertEqual(status, 400)

    async def test_fires_repull_nudge(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        with self._serve("light.a"):
            await self._put(hdr, ["light.a"])
        kinds = [
            data.get("kind")
            for evt, data in self.hass.bus.fired
            if evt == EVENT_REGISTRY_CHANGED
        ]
        self.assertIn("favorites", kinds)

    async def test_member_keyed_devices_share(self) -> None:
        # Two devices, SAME member_id → one favorites list.
        d1 = H.enroll(self.rt.auth, role="admin", member_id="mem-X")
        d2 = H.enroll(self.rt.auth, role="admin", member_id="mem-X")
        h1 = H.bearer(H.token_for(self.rt.auth, d1, role="admin"))
        h2 = H.bearer(H.token_for(self.rt.auth, d2, role="admin"))
        with self._serve("light.a"):
            await self._put(h1, ["light.a"])
            _, body = await self._get(h2)
        self.assertEqual(body["entity_ids"], ["light.a"])  # d2 sees d1's write

    async def test_member_keyed_devices_isolated(self) -> None:
        d1 = H.enroll(self.rt.auth, role="admin", member_id="mem-A")
        d2 = H.enroll(self.rt.auth, role="user", member_id="mem-B")
        h1 = H.bearer(H.token_for(self.rt.auth, d1, role="admin"))
        h2 = H.bearer(H.token_for(self.rt.auth, d2, role="user"))
        with self._serve("light.a"):
            await self._put(h1, ["light.a"])
            _, body = await self._get(h2)
        self.assertEqual(body["entity_ids"], [])  # different member → empty

    async def test_scoped_put_preserves_out_of_scope(self) -> None:
        # Phase 7: a room-scoped caller's replace must not delete favorites in
        # rooms it can't see.
        dev, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        member = self.rt.auth.member_id_for(dev)
        # Pre-existing: a(room1, in scope) + b(room2, OUT of scope).
        self.rt.registry.set_favorites(member, ["light.a", "light.b"])
        self.hass.states.add("light.a")
        self.hass.states.add("light.b")
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a", "light.b"]),
            in_scope=H.in_scope_for({"room1": {"light.a"}}),
        ):
            # The scoped caller re-submits only its in-scope slice (just a).
            status, body = await self._put(hdr, ["light.a"])
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_ids"], ["light.a"])  # echo in-scope only
        stored = self.rt.registry.get_favorites(member)
        self.assertIn("light.b", stored)  # out-of-scope PRESERVED
        self.assertIn("light.a", stored)


def _unpack(headers, rt):
    """The device id from a bearer header (decode the JWT sub claim)."""
    token = headers["Authorization"].removeprefix("Bearer ")
    import json
    import base64

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


if __name__ == "__main__":
    unittest.main()
