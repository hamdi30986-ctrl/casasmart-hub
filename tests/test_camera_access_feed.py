"""A member's camera access on every path a camera leaves the hub by, other
than the camera endpoints themselves (those are in test_camera_api):

* ``GET /devices`` and ``GET /devices/{id}``
* the WebSocket snapshot and state pushes
* ``GET /registry`` (the device projection)
* ``GET``/``PUT /me/favorites``
* ``GET``/``PUT /me/settings`` (widget tiles)
* ``GET /now`` (the favorites seed of the recents)

A member with "none" never sees a camera; "rooms" sees the cameras in their
rooms; "all" sees a camera outside their rooms while every other device
still follows the room scope. Needs a real Home Assistant (the views pull in
HA's recorder and registries), like the other view suites.
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
    from casasmart import ws as wsmod
    from casasmart.api import CasaSmartDevicesView, CasaSmartDeviceView
    from casasmart.registry_api import CasaSmartFavoritesView, CasaSmartRegistryView
    from casasmart.settings_api import CasaSmartUserSettingsView
    from casasmart.user_settings import UserSettingsEngine
    from test_ws_reauth import FakeSocket

    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR

CAM_IN = "camera.kitchen"
CAM_OUT = "camera.garage"
LIGHT_IN = "light.kitchen"
LIGHT_OUT = "light.garage"
ALL = (CAM_IN, CAM_OUT, LIGHT_IN, LIGHT_OUT)
SCOPE = {"kitchen": {CAM_IN, LIGHT_IN}, "garage": {CAM_OUT, LIGHT_OUT}}


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class CameraAccessFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.user_settings = UserSettingsEngine(
            self.rt.storage.table("user_settings")
        )
        for entity_id in ALL:
            self.hass.states.add(entity_id, state="on")
        serialize = lambda hass, state: {"entity_id": state.entity_id}  # noqa: E731
        for module in ("casasmart.api", "casasmart.ws"):
            patcher = mock.patch.multiple(
                module, is_served=H.is_served_for(ALL), serialize_device=serialize
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (
            ("casasmart.registry_api.is_served", H.is_served_for(ALL)),
            ("casasmart.settings_api.is_served", H.is_served_for(ALL)),
            ("casasmart.filtering.in_scope", H.in_scope_for(SCOPE)),
            ("casasmart.registry_api.is_assignable", lambda hass, eid: eid in ALL),
            ("casasmart.registry_api.area_id_of", lambda hass, eid: None),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _member(self, camera_access=None, rooms=("kitchen",)):
        rooms = list(rooms) if rooms is not None else None
        device_id = H.enroll(
            self.rt.auth, role="user", rooms=rooms, camera_access=camera_access
        )
        token = H.token_for(self.rt.auth, device_id, role="user", rooms=rooms)
        return device_id, H.bearer(token), token

    async def _devices(self, headers) -> list[str]:
        status, body = H.read_response(
            await CasaSmartDevicesView(self.hass).get(H.FakeRequest(headers=headers))
        )
        self.assertEqual(status, 200)
        return [device["entity_id"] for device in body["devices"]]

    async def _device(self, headers, entity_id) -> int:
        resp = await CasaSmartDeviceView(self.hass).get(
            H.FakeRequest(headers=headers), entity_id
        )
        return resp.status

    async def _registry_devices(self, headers) -> list[str]:
        status, body = H.read_response(
            await CasaSmartRegistryView(self.hass).get(H.FakeRequest(headers=headers))
        )
        self.assertEqual(status, 200)
        return [device["entity_id"] for device in body["devices"]]

    async def _ws_snapshot(self, token) -> list[str]:
        socket = FakeSocket()
        socket.client_sends({"type": "auth", "token": token})
        conn = wsmod.WsConnection(self.hass, socket, "test")
        self.assertTrue(await conn._authenticate_first_frame())
        for coro in (conn._sender_loop(), conn._receive_loop()):
            task = asyncio.create_task(coro)
            self.addCleanup(task.cancel)
        self.addCleanup(conn.cleanup)
        socket.client_sends({"type": "subscribe"})
        for _ in range(20):
            await asyncio.sleep(0)
        while len(conn._send_queue):
            await asyncio.sleep(0)
        snapshot = next(f for f in socket.sent if f["type"] == "subscribed")
        shown = sorted(d["entity_id"] for d in snapshot["devices"])
        # A state push for every entity: only the visible ones reach the app.
        socket.sent.clear()
        for entity_id in ALL:
            conn._on_state_changed(
                types.SimpleNamespace(
                    data={
                        "entity_id": entity_id,
                        "new_state": self.hass.states.get(entity_id),
                    }
                )
            )
        for _ in range(20):
            await asyncio.sleep(0)
        while len(conn._send_queue):
            await asyncio.sleep(0)
        pushed = sorted(
            f["device"]["entity_id"]
            for f in socket.sent
            if f["type"] == "state_changed"
        )
        self.assertEqual(pushed, shown)
        return shown

    async def _favorites(self, headers, entity_ids) -> tuple[int, list[str] | None]:
        view = CasaSmartFavoritesView(self.hass)
        status, body = H.read_response(
            await view.put(
                H.FakeRequest(headers=headers, body={"entity_ids": entity_ids})
            )
        )
        if status != 200:
            return status, body
        status, body = H.read_response(await view.get(H.FakeRequest(headers=headers)))
        return status, body["entity_ids"]

    async def _widget_tiles(self, headers, entity_ids) -> tuple[int, list[str] | None]:
        view = CasaSmartUserSettingsView(self.hass)
        tiles = [{"type": "toggle", "entityId": eid, "name": eid} for eid in entity_ids]
        status, body = H.read_response(
            await view.put(H.FakeRequest(headers=headers, body={"widget_tiles": tiles}))
        )
        if status != 200:
            return status, body
        status, body = H.read_response(await view.get(H.FakeRequest(headers=headers)))
        return status, [tile["entityId"] for tile in body["widget_tiles"]]

    # -- none -------------------------------------------------------------------

    async def test_none_hides_every_camera_everywhere(self) -> None:
        _, headers, token = self._member(camera_access="none")
        self.assertEqual(await self._devices(headers), [LIGHT_IN])
        self.assertEqual(await self._device(headers, CAM_IN), 404)
        self.assertEqual(await self._device(headers, LIGHT_IN), 200)
        self.assertEqual(await self._registry_devices(headers), [LIGHT_IN])
        self.assertEqual(await self._ws_snapshot(token), [LIGHT_IN])
        self.assertEqual((await self._favorites(headers, [CAM_IN]))[0], 400)
        self.assertEqual((await self._widget_tiles(headers, [CAM_IN]))[0], 400)

    async def test_none_on_an_unscoped_member_hides_only_cameras(self) -> None:
        _, headers, token = self._member(camera_access="none", rooms=None)
        self.assertEqual(await self._devices(headers), [LIGHT_OUT, LIGHT_IN])
        self.assertEqual(await self._ws_snapshot(token), [LIGHT_OUT, LIGHT_IN])

    # -- rooms ------------------------------------------------------------------

    async def test_rooms_shows_the_cameras_in_its_rooms(self) -> None:
        _, headers, token = self._member(camera_access="rooms")
        self.assertEqual(await self._devices(headers), [CAM_IN, LIGHT_IN])
        self.assertEqual(await self._device(headers, CAM_OUT), 404)
        self.assertEqual(await self._registry_devices(headers), [CAM_IN, LIGHT_IN])
        self.assertEqual(await self._ws_snapshot(token), [CAM_IN, LIGHT_IN])
        self.assertEqual(await self._favorites(headers, [CAM_IN]), (200, [CAM_IN]))
        self.assertEqual((await self._favorites(headers, [CAM_OUT]))[0], 400)
        self.assertEqual(await self._widget_tiles(headers, [CAM_IN]), (200, [CAM_IN]))

    async def test_a_record_from_before_the_field_behaves_like_rooms(self) -> None:
        _, headers, token = self._member()
        self.assertEqual(await self._devices(headers), [CAM_IN, LIGHT_IN])
        self.assertEqual(await self._ws_snapshot(token), [CAM_IN, LIGHT_IN])

    # -- all --------------------------------------------------------------------

    async def test_all_adds_the_cameras_outside_its_rooms_and_nothing_else(
        self,
    ) -> None:
        _, headers, token = self._member(camera_access="all")
        self.assertEqual(await self._devices(headers), [CAM_OUT, CAM_IN, LIGHT_IN])
        self.assertEqual(await self._device(headers, CAM_OUT), 200)
        self.assertEqual(await self._device(headers, LIGHT_OUT), 404)
        self.assertEqual(
            await self._registry_devices(headers), [CAM_OUT, CAM_IN, LIGHT_IN]
        )
        self.assertEqual(await self._ws_snapshot(token), [CAM_OUT, CAM_IN, LIGHT_IN])
        self.assertEqual(
            await self._favorites(headers, [CAM_OUT, LIGHT_IN]),
            (200, [CAM_OUT, LIGHT_IN]),
        )
        self.assertEqual((await self._favorites(headers, [LIGHT_OUT]))[0], 400)
        self.assertEqual(await self._widget_tiles(headers, [CAM_OUT]), (200, [CAM_OUT]))

    # -- the favorites a member can no longer see are kept, not erased -----------

    async def test_taking_cameras_away_hides_a_camera_favorite_without_erasing_it(
        self,
    ) -> None:
        device_id, headers, _ = self._member(camera_access="rooms")
        self.assertEqual(
            await self._favorites(headers, [CAM_IN, LIGHT_IN]),
            (200, [CAM_IN, LIGHT_IN]),
        )
        self.rt.auth.update_device(device_id, camera_access="none")
        cached = self.rt.auth._device_cache[device_id]
        fresh = H.bearer(
            H.auth_tokens.issue_token(
                self.rt.auth._signing_secret(),
                device_id,
                "user",
                ["kitchen"],
                3600,
                ver=cached["ver"],
            )
        )
        status, body = H.read_response(
            await CasaSmartFavoritesView(self.hass).get(H.FakeRequest(headers=fresh))
        )
        self.assertEqual((status, body["entity_ids"]), (200, [LIGHT_IN]))
        # Re-saving the visible list keeps the hidden camera for when the
        # access comes back.
        status, _ = H.read_response(
            await CasaSmartFavoritesView(self.hass).put(
                H.FakeRequest(headers=fresh, body={"entity_ids": [LIGHT_IN]})
            )
        )
        self.assertEqual(status, 200)
        member = self.rt.auth.member_id_for(device_id)
        self.assertEqual(
            sorted(self.rt.registry.get_favorites(member)), [CAM_IN, LIGHT_IN]
        )


if __name__ == "__main__":
    unittest.main()
