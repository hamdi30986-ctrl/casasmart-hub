"""A room's start profile: GET/PUT/DELETE /api/casasmart/now/rooms/{id}/start-profile.

The lights and fans one tap "on" turns on when the hub knows nothing better
(room_intel.one_tap). Driven through the real view with the real auth,
registry and NowDataEngine over a temp HubStorage (view_harness); only hass
and the request are fakes. Real-HA environment only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from casasmart.now_api import CasaSmartRoomStartProfileView
    from casasmart.registry_api import CasaSmartRoomView
    from homeassistant.helpers import entity_registry as er

    _ERR = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


class _NoEntityRegistry:
    def async_get(self, entity_id):
        return None


# entity id -> (room, attributes)
ENTITIES = {
    "light.majlis_main": ("majlis", {}),
    "light.majlis_spot": ("majlis", {}),
    "fan.majlis": ("majlis", {}),
    "light.majlis_lamp": ("majlis", {"casasmart_room_activity_exclude": True}),
    "switch.majlis_relay": ("majlis", {"device_class": "switch"}),
    "switch.majlis_outlet": ("majlis", {"device_class": "outlet"}),
    "climate.majlis_ac": ("majlis", {"hvac_modes": ["off", "cool"]}),
    "cover.majlis_curtain": ("majlis", {}),
    "media_player.majlis_tv": ("majlis", {}),
    "water_heater.majlis_tank": ("majlis", {}),
    "light.kitchen": ("kitchen", {}),
    "light.majlis_unserved": ("majlis", {}),
}
UNSERVED = {"light.majlis_unserved"}


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class StartProfileApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hass, self.rt = H.make_hub(self.tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        self.majlis = self.rt.registry.create_room("Majlis")["room_id"]
        self.kitchen = self.rt.registry.create_room("Kitchen")["room_id"]
        rooms = {"majlis": self.majlis, "kitchen": self.kitchen}
        for entity_id, (room, attributes) in ENTITIES.items():
            self.hass.states.add(entity_id, state="off", attributes=attributes)
            self.rt.registry.assign_device(entity_id, room_id=rooms[room])
        self.view = CasaSmartRoomStartProfileView(self.hass)
        served = H.is_served_for(set(ENTITIES) - UNSERVED)
        patcher = mock.patch.multiple("casasmart.now_api", is_served=served)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def send(self, method, room_id=None, body=None, *, role="admin", rooms=None):
        _, headers = H.session(self.rt.auth, role=role, rooms=rooms)
        handler = getattr(self.view, method)
        return H.read_response(
            await handler(
                H.FakeRequest(headers=headers, body=body),
                room_id=room_id or self.majlis,
            )
        )

    async def test_set_read_and_clear(self):
        self.assertEqual(
            await self.send("get"), (200, {"room_id": self.majlis, "entity_ids": []})
        )
        status, body = await self.send(
            "put", body={"entity_ids": ["light.majlis_main", "fan.majlis"]}
        )
        self.assertEqual(
            (status, body),
            (
                200,
                {
                    "room_id": self.majlis,
                    "entity_ids": ["light.majlis_main", "fan.majlis"],
                },
            ),
        )
        self.assertEqual((await self.send("get"))[1]["entity_ids"], body["entity_ids"])
        self.assertEqual(
            self.rt.now_data.start_profile(self.majlis),
            ["light.majlis_main", "fan.majlis"],
        )
        # Saved in the hub's own table (factory reset clears it).
        self.assertIn(self.majlis, self.rt.storage.table("now_room_start_profiles"))
        self.assertEqual(
            await self.send("delete"), (200, {"room_id": self.majlis, "entity_ids": []})
        )
        self.assertEqual(self.rt.now_data.start_profile(self.majlis), [])
        # Clearing twice is fine.
        self.assertEqual((await self.send("delete"))[0], 200)

    async def test_only_the_rooms_lights_and_fans(self):
        refused = {
            "switch.majlis_relay": "is not a light or fan",
            "switch.majlis_outlet": "is not a light or fan",
            "climate.majlis_ac": "is not a light or fan",
            "cover.majlis_curtain": "is not a light or fan",
            "media_player.majlis_tv": "is not a light or fan",
            "water_heater.majlis_tank": "is not a light or fan",
            "light.kitchen": "is not a light or fan in this room",
            "light.majlis_lamp": "is not a light or fan in this room",
            "light.majlis_unserved": "is not a light or fan in this room",
            "light.missing": "is not a light or fan in this room",
        }
        self.rt.now_data.set_start_profile(self.majlis, ["light.majlis_spot"])
        for entity_id, message in refused.items():
            with self.subTest(entity_id=entity_id):
                status, body = await self.send(
                    "put", body={"entity_ids": ["light.majlis_main", entity_id]}
                )
                self.assertEqual(status, 400)
                self.assertIn(entity_id, body["message"])
                self.assertIn(message, body["message"])
        # Nothing changed.
        self.assertEqual(
            self.rt.now_data.start_profile(self.majlis), ["light.majlis_spot"]
        )

    async def test_the_body_is_checked(self):
        for body in (
            {},
            {"entity_ids": []},
            {"entity_ids": "light.majlis_main"},
            {"entity_ids": ["light.majlis_main", "light.majlis_main"]},
            {"entity_ids": [7]},
            {"entity_ids": [f"light.l{i}" for i in range(65)]},
            {"entity_ids": ["light.majlis_main"], "extra": True},
        ):
            with self.subTest(body=body):
                status, _ = await self.send("put", body=body)
                self.assertEqual(status, 400)
        self.assertEqual(self.rt.now_data.start_profile(self.majlis), [])

    async def test_installers_may_admins_may_members_may_not(self):
        body = {"entity_ids": ["light.majlis_main"]}
        self.assertEqual((await self.send("put", body=body, role="sub-admin"))[0], 200)
        for method in ("get", "put", "delete"):
            with self.subTest(method=method):
                status, _ = await self.send(method, body=body, role="user")
                self.assertEqual(status, 403)

    async def test_a_room_outside_the_scope_or_unknown_is_404(self):
        body = {"entity_ids": ["light.majlis_main"]}
        for method in ("get", "put", "delete"):
            with self.subTest(method=method):
                scoped = await self.send(
                    method, body=body, role="sub-admin", rooms=[self.kitchen]
                )
                self.assertEqual(scoped[0], 404)
                unknown = await self.send(method, room_id="room-nope", body=body)
                self.assertEqual(unknown[0], 404)
        self.assertEqual(self.rt.now_data.start_profile(self.majlis), [])

    async def test_a_deleted_room_takes_its_start_profile_with_it(self):
        self.rt.now_data.set_start_profile(self.majlis, ["light.majlis_main"])
        self.rt.now_data.set_start_profile(self.kitchen, ["light.kitchen"])
        _, headers = H.session(self.rt.auth, role="admin")
        with mock.patch.multiple(
            "casasmart.registry_api", is_served=H.is_served_for([])
        ):
            resp = await CasaSmartRoomView(self.hass).delete(
                H.FakeRequest(headers=headers), room_id=self.majlis
            )
        self.assertEqual(H.read_response(resp)[0], 200)
        self.assertEqual(self.rt.now_data.start_profile(self.majlis), [])
        self.assertNotIn(self.majlis, self.rt.storage.table("now_room_start_profiles"))
        self.assertEqual(
            self.rt.now_data.start_profile(self.kitchen), ["light.kitchen"]
        )


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class StartProfileRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_put_and_delete_on_its_own_path(self):
        hass = type("Hass", (), {"data": {}})()
        app = web.Application()
        CasaSmartRoomStartProfileView(hass).register(hass, app, app.router)
        path = "/api/casasmart/now/rooms/room-1/start-profile"
        for method, expected in (
            ("GET", None),
            ("PUT", None),
            ("DELETE", None),
            ("POST", 405),
        ):
            with self.subTest(method=method):
                match = await app.router.resolve(
                    make_mocked_request(method, path, app=app)
                )
                exception = match.http_exception
                self.assertEqual(
                    None if exception is None else exception.status, expected
                )


if __name__ == "__main__":
    unittest.main()
