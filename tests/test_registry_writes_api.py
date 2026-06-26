"""View-layer tests for ``registry_api`` — the STRUCTURE write surface.

Favorites live in ``test_registry_api``; this suite pins the floors/rooms/
scenes CRUD, the device-assignment PATCH/DELETE, the registry FEED projection,
and the auth gates. The crown jewel is the Phase-1 assignment fix: the device
PATCH gates on ``is_assignable`` (registry-exists), NOT ``is_served`` — so an
entity the integration HID (hidden_by set, is_served False) but that EXISTS in
the registry stays editable, while a genuine ghost (no registry, no state) 404s.

Container/CI only (imports Home Assistant). Run:
    docker exec -w /config/tests homeassistant-dev \
        python3 -m unittest test_registry_writes_api -v
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
    from casasmart.registry_api import (
        CasaSmartRegistryView,
        CasaSmartFloorsView,
        CasaSmartFloorView,
        CasaSmartRoomsView,
        CasaSmartRoomView,
        CasaSmartDeviceAssignmentView,
        CasaSmartScenesView,
        CasaSmartSceneView,
    )
    from casasmart.const import EVENT_REGISTRY_CHANGED

    _ERR = None
except Exception as err:  # noqa: BLE001 — HA-free local env
    CasaSmartRegistryView = CasaSmartFloorsView = CasaSmartFloorView = None
    CasaSmartRoomsView = CasaSmartRoomView = CasaSmartDeviceAssignmentView = None
    CasaSmartScenesView = CasaSmartSceneView = EVENT_REGISTRY_CHANGED = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


def _all_in_scope(hass, eid, scope):
    return scope is None


def _assignable_for(assignable):
    """Fake ``is_assignable(hass, eid)`` -> True iff eid ∈ assignable."""
    assignable = set(assignable)
    return lambda hass, eid: eid in assignable


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class RegistryWritesTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)

    def _kinds(self, kind):
        return [
            data.get("kind")
            for evt, data in self.hass.bus.fired
            if evt == EVENT_REGISTRY_CHANGED
        ].count(kind)


# --------------------------------------------------------------------------- #
# Floors CRUD
# --------------------------------------------------------------------------- #
class FloorsCrud(RegistryWritesTestCase):
    async def test_create_patch_delete_roundtrip(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        create = CasaSmartFloorsView(self.hass)
        resp = await create.post(
            H.FakeRequest(headers=hdr, body={"name": "Ground", "sort_order": 2})
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 201)
        floor_id = body["floor_id"]
        self.assertEqual(body["name"], "Ground")
        # Stored for real.
        stored = {f["floor_id"]: f for f in self.rt.registry.list_floors()}
        self.assertEqual(stored[floor_id]["name"], "Ground")
        self.assertEqual(self._kinds("floors"), 1)

        # PATCH rename.
        view = CasaSmartFloorView(self.hass)
        resp = await view.patch(
            H.FakeRequest(headers=hdr, body={"name": "First"}),
            floor_id=floor_id,
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "First")
        stored = {f["floor_id"]: f for f in self.rt.registry.list_floors()}
        self.assertEqual(stored[floor_id]["name"], "First")

        # DELETE.
        resp = await view.delete(
            H.FakeRequest(headers=hdr), floor_id=floor_id
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], floor_id)
        self.assertEqual(self.rt.registry.list_floors(), [])

    async def test_patch_unknown_floor_is_404(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        view = CasaSmartFloorView(self.hass)
        resp = await view.patch(
            H.FakeRequest(headers=hdr, body={"name": "X"}),
            floor_id="floor-nope",
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_create_blank_name_is_400(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        view = CasaSmartFloorsView(self.hass)
        resp = await view.post(
            H.FakeRequest(headers=hdr, body={"name": "   "})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- #
# Rooms CRUD (incl. floor linkage + name bounds)
# --------------------------------------------------------------------------- #
class RoomsCrud(RegistryWritesTestCase):
    async def _make_floor(self, hdr) -> str:
        resp = await CasaSmartFloorsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "Ground"})
        )
        _, body = H.read_response(resp)
        return body["floor_id"]

    async def test_create_with_floor_then_patch_then_delete(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        floor_id = await self._make_floor(hdr)

        resp = await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(
                headers=hdr, body={"name": "Kitchen", "floor_id": floor_id}
            )
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 201)
        room_id = body["room_id"]
        self.assertEqual(body["floor_id"], floor_id)
        stored = {r["room_id"]: r for r in self.rt.registry.list_rooms()}
        self.assertEqual(stored[room_id]["name"], "Kitchen")
        self.assertEqual(stored[room_id]["floor_id"], floor_id)

        # PATCH rename + clear floor.
        view = CasaSmartRoomView(self.hass)
        resp = await view.patch(
            H.FakeRequest(
                headers=hdr, body={"name": "Galley", "floor_id": None}
            ),
            room_id=room_id,
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Galley")
        self.assertIsNone(body["floor_id"])
        stored = {r["room_id"]: r for r in self.rt.registry.list_rooms()}
        self.assertEqual(stored[room_id]["name"], "Galley")
        self.assertIsNone(stored[room_id]["floor_id"])

        # DELETE.
        resp = await view.delete(
            H.FakeRequest(headers=hdr), room_id=room_id
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], room_id)
        self.assertEqual(self.rt.registry.list_rooms(), [])

    async def test_create_unknown_floor_is_400(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        resp = await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(
                headers=hdr, body={"name": "Bath", "floor_id": "floor-ghost"}
            )
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_create_overlong_name_is_400(self) -> None:
        # _NAME_MAX is 64.
        _, hdr = H.session(self.rt.auth, role="admin")
        resp = await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "x" * 65})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 400)

    async def test_delete_unknown_room_is_404(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        # No served states, so the ha_orphans scan is empty.
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for([]),
        ):
            resp = await CasaSmartRoomView(self.hass).delete(
                H.FakeRequest(headers=hdr), room_id="room-nope"
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_delete_floor_in_use_is_409(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        floor_id = await self._make_floor(hdr)
        await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(
                headers=hdr, body={"name": "Den", "floor_id": floor_id}
            )
        )
        resp = await CasaSmartFloorView(self.hass).delete(
            H.FakeRequest(headers=hdr), floor_id=floor_id
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 409)  # InUseError -> CONFLICT


# --------------------------------------------------------------------------- #
# Device assignment PATCH/DELETE — THE Phase-1 is_assignable regression
# --------------------------------------------------------------------------- #
class DeviceAssignment(RegistryWritesTestCase):
    async def test_patch_hidden_but_registered_entity_succeeds(self) -> None:
        """The Phase-1 fix: a HIDDEN entity (is_served False) that still EXISTS
        in the registry (is_assignable True) must remain assignable."""
        _, hdr = H.session(self.rt.auth, role="admin")
        # Pre-create a room to assign into.
        rr = await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "Hall"})
        )
        _, rbody = H.read_response(rr)
        room_id = rbody["room_id"]

        eid = "switch.gang2"  # secondary gang the integration hid
        view = CasaSmartDeviceAssignmentView(self.hass)
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_assignable=_assignable_for([eid]),  # exists in registry
            is_served=H.is_served_for([]),  # but HIDDEN from the feed
            in_scope=_all_in_scope,
        ):
            resp = await view.patch(
                H.FakeRequest(
                    headers=hdr,
                    body={"room_id": room_id, "display_name": "Fan"},
                ),
                entity_id=eid,
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_id"], eid)
        self.assertEqual(body["room_id"], room_id)
        self.assertEqual(body["display_name"], "Fan")
        # Stored for real.
        self.assertEqual(
            self.rt.registry.list_assignments()[eid]["room_id"], room_id
        )
        self.assertEqual(self._kinds("devices"), 1)

    async def test_patch_genuine_ghost_is_404(self) -> None:
        """No registry entry + no state = a ghost. is_assignable False -> 404,
        and NOTHING is written."""
        _, hdr = H.session(self.rt.auth, role="admin")
        eid = "light.never_existed"
        view = CasaSmartDeviceAssignmentView(self.hass)
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_assignable=_assignable_for([]),  # not assignable
            is_served=H.is_served_for([]),
            in_scope=_all_in_scope,
        ):
            resp = await view.patch(
                H.FakeRequest(headers=hdr, body={"display_name": "X"}),
                entity_id=eid,
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)
        self.assertNotIn(eid, self.rt.registry.list_assignments())
        self.assertEqual(self._kinds("devices"), 0)

    async def test_scoped_subadmin_out_of_scope_is_404(self) -> None:
        """A room-scoped caller editing an in-registry but out-of-scope entity
        gets the same 404 (no enumeration)."""
        dev, hdr = H.session(self.rt.auth, role="sub-admin", rooms=["room1"])
        eid = "switch.elsewhere"
        view = CasaSmartDeviceAssignmentView(self.hass)
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_assignable=_assignable_for([eid]),
            is_served=H.is_served_for([eid]),
            in_scope=H.in_scope_for({"room1": set()}),  # not in scope
        ):
            resp = await view.patch(
                H.FakeRequest(headers=hdr, body={"display_name": "X"}),
                entity_id=eid,
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)

    async def test_delete_assignment_reverts_to_ha_area(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        rr = await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "Hall"})
        )
        _, rbody = H.read_response(rr)
        room_id = rbody["room_id"]
        eid = "light.a"
        # Seed an assignment record directly.
        self.rt.registry.assign_device(eid, room_id=room_id)
        view = CasaSmartDeviceAssignmentView(self.hass)
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_assignable=_assignable_for([eid]),
            is_served=H.is_served_for([eid]),
            in_scope=_all_in_scope,
        ):
            resp = await view.delete(
                H.FakeRequest(headers=hdr), entity_id=eid
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], eid)
        self.assertNotIn(eid, self.rt.registry.list_assignments())
        self.assertEqual(self._kinds("devices"), 1)


# --------------------------------------------------------------------------- #
# Scenes CRUD
# --------------------------------------------------------------------------- #
class ScenesCrud(RegistryWritesTestCase):
    async def test_create_patch_delete_roundtrip(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        self.hass.states.add("switch.a")
        entities = [{"entity_id": "switch.a", "action": "turn_on"}]
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["switch.a"]),
        ):
            resp = await CasaSmartScenesView(self.hass).post(
                H.FakeRequest(
                    headers=hdr, body={"name": "Movie", "entities": entities}
                )
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 201)
        scene_id = body["scene_id"]
        self.assertEqual(body["name"], "Movie")
        stored = {s["scene_id"]: s for s in self.rt.registry.list_scenes()}
        self.assertEqual(stored[scene_id]["name"], "Movie")
        self.assertEqual(self._kinds("scenes"), 1)

        # PATCH rename (no entities change → no served gate).
        view = CasaSmartSceneView(self.hass)
        resp = await view.patch(
            H.FakeRequest(headers=hdr, body={"name": "Cinema"}),
            scene_id=scene_id,
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Cinema")
        stored = {s["scene_id"]: s for s in self.rt.registry.list_scenes()}
        self.assertEqual(stored[scene_id]["name"], "Cinema")

        # DELETE.
        resp = await view.delete(
            H.FakeRequest(headers=hdr), scene_id=scene_id
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], scene_id)
        self.assertEqual(self.rt.registry.list_scenes(), [])

    async def test_create_scene_unserved_entity_is_404(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        self.hass.states.add("switch.a")  # exists but not served
        entities = [{"entity_id": "switch.a", "action": "turn_on"}]
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for([]),  # nothing served
        ):
            resp = await CasaSmartScenesView(self.hass).post(
                H.FakeRequest(
                    headers=hdr, body={"name": "Bad", "entities": entities}
                )
            )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)
        self.assertEqual(self.rt.registry.list_scenes(), [])

    async def test_patch_unknown_scene_is_404(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        resp = await CasaSmartSceneView(self.hass).patch(
            H.FakeRequest(headers=hdr, body={"name": "X"}),
            scene_id="scene-nope",
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 404)


# --------------------------------------------------------------------------- #
# The registry FEED GET — the device list the app reads
# --------------------------------------------------------------------------- #
class RegistryFeed(RegistryWritesTestCase):
    async def test_feed_projects_served_devices(self) -> None:
        _, hdr = H.session(self.rt.auth, role="admin")
        floor_id = self.rt.registry.create_floor("Ground")["floor_id"]
        room_id = self.rt.registry.create_room("Kitchen", floor_id)["room_id"]
        self.hass.states.add("light.a")
        self.hass.states.add("switch.hidden")  # exists but NOT served
        self.rt.registry.assign_device(
            "light.a", room_id=room_id, display_name="Lamp", sort_order=3
        )
        view = CasaSmartRegistryView(self.hass)
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a"]),  # hidden NOT served
            in_scope=_all_in_scope,
            is_assignable=_assignable_for(["light.a"]),
            area_id_of=lambda hass, eid: room_id,
        ):
            resp = await view.get(H.FakeRequest(headers=hdr))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        # Projection shape.
        self.assertEqual(
            set(body.keys()),
            {"floors", "rooms", "devices", "scenes", "user_devices"},
        )
        eids = [d["entity_id"] for d in body["devices"]]
        self.assertEqual(eids, ["light.a"])  # unserved dropped
        device = body["devices"][0]
        self.assertEqual(
            set(device.keys()),
            {"entity_id", "room_id", "display_name", "sort_order"},
        )
        self.assertEqual(device["room_id"], room_id)
        self.assertEqual(device["display_name"], "Lamp")
        self.assertEqual(device["sort_order"], 3)
        # Floors/rooms carried through.
        self.assertEqual([f["floor_id"] for f in body["floors"]], [floor_id])
        self.assertEqual([r["room_id"] for r in body["rooms"]], [room_id])

    async def test_feed_scopes_to_caller_rooms(self) -> None:
        dev, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        # Two HA rooms; caller only owns room1.
        r1 = self.rt.registry.create_room("R1")["room_id"]
        r2 = self.rt.registry.create_room("R2")["room_id"]
        # Token carries literal "room1"; map our entity into scope manually.
        self.hass.states.add("light.in")
        self.hass.states.add("light.out")
        view = CasaSmartRegistryView(self.hass)
        # Scope is ["room1"]; rooms in registry are r1/r2 so the room slice is
        # empty — what we assert is the per-entity in_scope device filter.
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.in", "light.out"]),
            in_scope=H.in_scope_for({"room1": {"light.in"}}),
            is_assignable=_assignable_for([]),
            area_id_of=lambda hass, eid: None,
        ):
            resp = await view.get(H.FakeRequest(headers=hdr))
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        eids = [d["entity_id"] for d in body["devices"]]
        self.assertEqual(eids, ["light.in"])  # out-of-scope dropped

    async def test_feed_drops_grouped_device_with_out_of_scope_gang(self) -> None:
        # A grouped (multi-gang) record where ONE gang is out of the caller's
        # scope is dropped ENTIRELY — a partial projection would leak the
        # suffix-keyed gang_names/custom_name of the out-of-scope gang.
        _, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        self.hass.states.add("light.a")
        self.hass.states.add("light.b")
        self.rt.registry.upsert_user_device(
            "grp-1",
            entity_ids=["light.a", "light.b"],
            gang_names={"light.b": "SECRET-MASTER-BEDROOM"},
            custom_name="Hidden Group",
        )
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a", "light.b"]),
            in_scope=H.in_scope_for({"room1": {"light.a"}}),  # b OUT of scope
            is_assignable=_assignable_for(["light.a", "light.b"]),
            area_id_of=lambda hass, eid: None,
        ):
            resp = await CasaSmartRegistryView(self.hass).get(
                H.FakeRequest(headers=hdr)
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["user_devices"], [])  # all-or-nothing drop
        self.assertNotIn("SECRET-MASTER-BEDROOM", str(body))  # no gang-name leak

    async def test_feed_keeps_grouped_device_fully_in_scope(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user", rooms=["room1"])
        self.hass.states.add("light.a")
        self.hass.states.add("light.b")
        self.rt.registry.upsert_user_device(
            "grp-2", entity_ids=["light.a", "light.b"], custom_name="Both Here"
        )
        with mock.patch.multiple(
            "casasmart.registry_api",
            is_served=H.is_served_for(["light.a", "light.b"]),
            in_scope=H.in_scope_for({"room1": {"light.a", "light.b"}}),  # both in
            is_assignable=_assignable_for(["light.a", "light.b"]),
            area_id_of=lambda hass, eid: None,
        ):
            resp = await CasaSmartRegistryView(self.hass).get(
                H.FakeRequest(headers=hdr)
            )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["user_devices"]), 1)  # fully in-scope -> served


# --------------------------------------------------------------------------- #
# AUTH — writes require registry.manage (admin + sub-admin); user lacks it.
# --------------------------------------------------------------------------- #
class AuthGate(RegistryWritesTestCase):
    async def test_floor_create_no_token_is_401(self) -> None:
        resp = await CasaSmartFloorsView(self.hass).post(
            H.FakeRequest(headers={}, body={"name": "X"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 401)

    async def test_floor_create_user_role_is_403(self) -> None:
        # user is NOT in PERMISSIONS["registry.manage"].
        _, hdr = H.session(self.rt.auth, role="user")
        resp = await CasaSmartFloorsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "X"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)

    async def test_room_create_user_role_is_403(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user")
        resp = await CasaSmartRoomsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "X"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)

    async def test_scene_create_user_role_is_403(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user")
        resp = await CasaSmartScenesView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "X", "entities": []})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)

    async def test_device_patch_user_role_is_403(self) -> None:
        _, hdr = H.session(self.rt.auth, role="user")
        resp = await CasaSmartDeviceAssignmentView(self.hass).patch(
            H.FakeRequest(headers=hdr, body={"display_name": "X"}),
            entity_id="light.a",
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 403)

    async def test_subadmin_may_create_floor(self) -> None:
        # sub-admin IS in registry.manage → 201, proving the 403s above are
        # role-specific, not a blanket block.
        _, hdr = H.session(self.rt.auth, role="sub-admin")
        resp = await CasaSmartFloorsView(self.hass).post(
            H.FakeRequest(headers=hdr, body={"name": "SA"})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 201)

    async def test_feed_get_no_token_is_401(self) -> None:
        resp = await CasaSmartRegistryView(self.hass).get(
            H.FakeRequest(headers={})
        )
        status, _ = H.read_response(resp)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
