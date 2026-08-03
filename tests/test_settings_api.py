"""View-layer tests for ``settings_api`` — the per-person settings contract.

Pins the wire seam of ``GET/PUT /api/casasmart/me/settings`` (mini-block
MB-2): member-keyed read/write (Phase 5 — a person's devices share one doc,
different people are isolated), unknown-field + validation rejection (400),
and the settings re-pull nudge on a successful PUT.

The harness ``_Runtime`` does not expose ``user_settings`` (the view reads
``runtime_data.user_settings``), so asyncSetUp constructs a real
``UserSettingsEngine`` over ``runtime.storage.table("user_settings")`` and
injects it — exactly the engine the view's ``get_user_settings`` resolves.

Container/CI only (imports Home Assistant). Run:
    docker exec homeassistant python3 -m unittest tests.test_settings_api -v
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
    from casasmart.settings_api import CasaSmartUserSettingsView
    from casasmart.user_settings import UserSettingsEngine
    from casasmart.const import EVENT_REGISTRY_CHANGED

    _ERR = None
except Exception as err:  # noqa: BLE001 — HA-free local env
    CasaSmartUserSettingsView = UserSettingsEngine = EVENT_REGISTRY_CHANGED = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class SettingsViewTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        # The view resolves runtime_data.user_settings; the harness Runtime
        # doesn't ship one, so wire a real engine over the same temp storage.
        self.settings = UserSettingsEngine(self.rt.storage.table("user_settings"))
        self.rt.user_settings = self.settings
        self.view = CasaSmartUserSettingsView(self.hass)

    async def _get(self, headers):
        resp = await self.view.get(H.FakeRequest(headers=headers))
        return H.read_response(resp)

    async def _put(self, headers, body):
        resp = await self.view.put(H.FakeRequest(headers=headers, body=body))
        return H.read_response(resp)


class SettingsGet(SettingsViewTestCase):
    async def test_returns_member_doc(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        # Seed via the engine directly (as a PUT would land it).
        self.settings.update(member, {"display_name": "Alex"})
        status, body = await self._get(hdr)
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], "Alex")
        # The doc reports every known field, None when unset.
        self.assertIn("widget_tiles", body)
        self.assertIsNone(body["widget_tiles"])

    async def test_unset_member_is_empty_doc(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user")
        status, body = await self._get(hdr)
        self.assertEqual(status, 200)
        self.assertIsNone(body["display_name"])
        self.assertIsNone(body["widget_tiles"])

    async def test_no_token_is_401(self) -> None:
        status, _ = await self._get({})
        self.assertEqual(status, 401)


class SettingsPut(SettingsViewTestCase):
    async def test_updates_display_name_and_tiles(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        # A tank pseudo-tile round-trips without needing a served HA entity
        # (entity-tiles now ride the Phase-8 served+scope write-guard).
        tiles = [{"type": "tank", "entityId": "tank_1", "name": "Tank"}]
        status, body = await self._put(
            hdr, {"display_name": "Sara", "widget_tiles": tiles}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], "Sara")
        self.assertEqual(body["widget_tiles"], tiles)
        # Stored for real, member-keyed.
        stored = self.settings.get(member)
        self.assertEqual(stored["display_name"], "Sara")
        self.assertEqual(stored["widget_tiles"], tiles)

    async def test_partial_update_preserves_other_field(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        self.settings.update(member, {"display_name": "Keep"})
        # Write only widget_tiles — display_name must survive.
        status, body = await self._put(
            hdr, {"widget_tiles": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], "Keep")

    async def test_member_keyed_devices_share(self) -> None:
        # Two devices, SAME member_id -> one settings doc.
        d1 = H.enroll(self.rt.auth, role="admin", member_id="mem-X")
        d2 = H.enroll(self.rt.auth, role="admin", member_id="mem-X")
        h1 = H.bearer(H.token_for(self.rt.auth, d1, role="admin"))
        h2 = H.bearer(H.token_for(self.rt.auth, d2, role="admin"))
        await self._put(h1, {"display_name": "Shared"})
        _, body = await self._get(h2)
        self.assertEqual(body["display_name"], "Shared")  # d2 sees d1's write

    async def test_member_keyed_devices_isolated(self) -> None:
        d1 = H.enroll(self.rt.auth, role="admin", member_id="mem-A")
        d2 = H.enroll(self.rt.auth, role="user", member_id="mem-B")
        h1 = H.bearer(H.token_for(self.rt.auth, d1, role="admin"))
        h2 = H.bearer(H.token_for(self.rt.auth, d2, role="user"))
        await self._put(h1, {"display_name": "OnlyA"})
        _, body = await self._get(h2)
        self.assertIsNone(body["display_name"])  # different member -> isolated

    async def test_rejects_unknown_field(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        status, _ = await self._put(hdr, {"bogus_key": "x"})
        self.assertEqual(status, 400)

    async def test_rejects_overlong_display_name(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        status, _ = await self._put(hdr, {"display_name": "z" * 65})
        self.assertEqual(status, 400)

    async def test_rejects_malformed_widget_tiles(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        # Not a list -> rejected by the tiles validator.
        status, _ = await self._put(hdr, {"widget_tiles": "not-a-list"})
        self.assertEqual(status, 400)

    async def test_rejects_tile_missing_entity_id(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        bad = [{"type": "toggle", "entityId": "", "name": "x"}]
        status, _ = await self._put(hdr, {"widget_tiles": bad})
        self.assertEqual(status, 400)

    async def test_empty_body_is_400(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        status, _ = await self._put(hdr, {})
        self.assertEqual(status, 400)

    async def test_non_object_body_is_400(self) -> None:
        # json_body returns None for a non-dict payload -> view 400s.
        _, hdr = H.session(self.rt.auth, role="admin")
        resp = await self.view.put(H.FakeRequest(headers=hdr, body=["a"]))
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_fires_settings_repull_nudge(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        await self._put(hdr, {"display_name": "Nudge"})
        kinds = [
            data.get("kind")
            for evt, data in self.hass.bus.fired
            if evt == EVENT_REGISTRY_CHANGED
        ]
        self.assertIn("settings", kinds)

    async def test_rejected_put_fires_no_nudge(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        await self._put(hdr, {"bogus_key": "x"})
        fired = [evt for evt, _ in self.hass.bus.fired if evt == EVENT_REGISTRY_CHANGED]
        self.assertEqual(fired, [])

    async def test_no_token_is_401(self) -> None:
        status, _ = await self._put({}, {"display_name": "x"})
        self.assertEqual(status, 401)


class WidgetTileFilter(SettingsViewTestCase):
    """Phase 8 — widget_tiles get the favorites posture: phantom entity-tiles
    are filtered without mutation on GET, served+scope write-guard on PUT;
    non-HA pseudo-tiles (tank/scene/security) pass through untouched."""

    async def test_get_filters_phantom_without_mutating_storage(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        self.settings.update(member, {"widget_tiles": [
            {"type": "toggle", "entityId": "light.a", "name": "A"},
            {"type": "toggle", "entityId": "switch.gone", "name": "Gone"},
        ]})
        self.hass.states.add("light.a")  # switch.gone has no state (deleted)
        with mock.patch.multiple(
            "casasmart.settings_api",
            is_served=H.is_served_for(["light.a"]),
            in_scope=H.in_scope_for(),
        ):
            status, body = await self._get(hdr)
        self.assertEqual(status, 200)
        self.assertEqual([t["entityId"] for t in body["widget_tiles"]], ["light.a"])
        # The absent state may be a startup transient, so GET keeps storage.
        stored = self.settings.get(member)["widget_tiles"]
        self.assertEqual(
            [t["entityId"] for t in stored],
            ["light.a", "switch.gone"],
        )

    async def test_get_keeps_pseudo_tiles(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="admin")
        member = self.rt.auth.member_id_for(dev)
        # tank + scene ids are NOT served HA entities, yet must survive.
        self.settings.update(member, {"widget_tiles": [
            {"type": "tank", "entityId": "tank_1", "name": "Tank"},
            {"type": "scene", "entityId": "scene-xyz", "name": "Movie"},
        ]})
        with mock.patch.multiple(
            "casasmart.settings_api",
            is_served=H.is_served_for([]),  # nothing served
            in_scope=H.in_scope_for(),
        ):
            status, body = await self._get(hdr)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["widget_tiles"]), 2)  # both pseudo-tiles kept

    async def test_get_scopes_entity_tiles_without_healing_out_of_scope(self):
        dev, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        member = self.rt.auth.member_id_for(dev)
        self.settings.update(member, {"widget_tiles": [
            {"type": "toggle", "entityId": "light.a", "name": "A"},
            {"type": "toggle", "entityId": "light.b", "name": "B"},
        ]})
        self.hass.states.add("light.a")
        self.hass.states.add("light.b")
        with mock.patch.multiple(
            "casasmart.settings_api",
            is_served=H.is_served_for(["light.a", "light.b"]),
            in_scope=H.in_scope_for({"room1": {"light.a"}}),  # b out of scope
        ):
            status, body = await self._get(hdr)
        self.assertEqual([t["entityId"] for t in body["widget_tiles"]], ["light.a"])
        # Out-of-scope b is alive — must NOT be self-healed away.
        stored = [t["entityId"] for t in self.settings.get(member)["widget_tiles"]]
        self.assertEqual(stored, ["light.a", "light.b"])

    async def test_put_rejects_unserved_entity_tile(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        self.hass.states.add("light.a")
        with mock.patch.multiple(
            "casasmart.settings_api",
            is_served=H.is_served_for(["light.a"]),
            in_scope=H.in_scope_for(),
        ):
            status, _ = await self._put(hdr, {"widget_tiles": [
                {"type": "toggle", "entityId": "switch.bogus", "name": "X"},
            ]})
        self.assertEqual(status, 400)

    async def test_put_rejects_out_of_scope_entity_tile(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        self.hass.states.add("light.b")
        with mock.patch.multiple(
            "casasmart.settings_api",
            is_served=H.is_served_for(["light.b"]),
            in_scope=H.in_scope_for({"room1": {"light.a"}}),  # b out of scope
        ):
            status, _ = await self._put(hdr, {"widget_tiles": [
                {"type": "toggle", "entityId": "light.b", "name": "B"},
            ]})
        self.assertEqual(status, 400)

    async def test_put_accepts_served_entity_plus_pseudo_tiles(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        self.hass.states.add("light.a")
        with mock.patch.multiple(
            "casasmart.settings_api",
            is_served=H.is_served_for(["light.a"]),
            in_scope=H.in_scope_for(),
        ):
            status, body = await self._put(hdr, {"widget_tiles": [
                {"type": "toggle", "entityId": "light.a", "name": "A"},
                {"type": "tank", "entityId": "tank_1", "name": "Tank"},
            ]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["widget_tiles"]), 2)


if __name__ == "__main__":
    unittest.main()
