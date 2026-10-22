"""View-layer tests for per-member camera access and room-scope hygiene.

Pins the WIRE seam of the user-management surface:

* ``GET /users`` reports every device's effective ``camera_access`` and drops
  room ids that no longer exist from the scopes it lists.
* ``PATCH /users/{id}`` takes ``camera_access`` (none / rooms / all), refuses
  an unknown value, and refuses a scope naming a room that doesn't exist.
* ``POST /pairing/codes`` bakes ``camera_access`` into a code, refuses an
  unknown room, and "add a device" copies the member's current access.
* ``POST /auth/enroll`` stores the code's access and reports the effective
  value; ``POST /auth/token`` and ``GET /auth/whoami`` tell a device its own
  rooms and camera access (older apps ignore the extra fields).

The engines are REAL (AuthEngine, PairingManager, RegistryEngine over a temp
HubStorage); only ``hass`` + the request are the ``view_harness`` fakes, and
``homeassistant`` comes from the shared stub package, so this suite runs
locally AND in the container.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart.auth_api import (  # noqa: E402
    CasaSmartChallengeView,
    CasaSmartEnrollView,
    CasaSmartPairingCodesView,
    CasaSmartTokenView,
    CasaSmartUsersView,
    CasaSmartUserView,
    CasaSmartWhoamiView,
)
from casasmart.const import EVENT_AUTH_CHANGED  # noqa: E402
from casasmart.pairing import PairingManager  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

LAN_IP = "192.168.1.50"


def _keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_key, pem


class _MemberAccessTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.rt.pairing = PairingManager(
            self.rt.storage.table("pairing_codes"), self.rt.auth.has_admin
        )
        self.rt.auth.warm_up()
        self.kitchen = self.rt.registry.create_room("Kitchen")["room_id"]
        self.garage = self.rt.registry.create_room("Garage")["room_id"]
        _, self.admin = H.session(self.rt.auth, role="admin")

    async def _users(self):
        resp = await CasaSmartUsersView(self.hass).get(
            H.FakeRequest(headers=self.admin)
        )
        status, body = H.read_response(resp)
        self.assertEqual(status, 200)
        return {user["device_id"]: user for user in body["users"]}

    async def _patch(self, device_id, body):
        resp = await CasaSmartUserView(self.hass).patch(
            H.FakeRequest(headers=self.admin, body=body), device_id
        )
        return H.read_response(resp)

    async def _mint(self, body):
        resp = await CasaSmartPairingCodesView(self.hass).post(
            H.FakeRequest(headers=self.admin, body=body, remote=LAN_IP)
        )
        return H.read_response(resp)


class UsersListTests(_MemberAccessTestCase):
    async def test_reports_the_effective_camera_access(self) -> None:
        owner = H.enroll(self.rt.auth, role="admin")
        unscoped = H.enroll(self.rt.auth, role="user")
        scoped = H.enroll(self.rt.auth, role="user", rooms=[self.kitchen])
        explicit = H.enroll(
            self.rt.auth, role="user", rooms=[self.kitchen], camera_access="none"
        )
        users = await self._users()
        self.assertEqual(users[owner]["camera_access"], "all")
        self.assertEqual(users[unscoped]["camera_access"], "all")
        self.assertEqual(users[scoped]["camera_access"], "rooms")
        self.assertEqual(users[explicit]["camera_access"], "none")

    async def test_drops_dead_room_ids_from_every_scope(self) -> None:
        # A scope left holding a room that was deleted (or never existed).
        kid = H.enroll(self.rt.auth, role="user", rooms=[self.kitchen, "room-gone"])
        guest = H.enroll(self.rt.auth, role="user", rooms=["room-gone"])
        users = await self._users()
        self.assertEqual(users[kid]["rooms"], [self.kitchen])
        self.assertEqual(users[guest]["rooms"], [])  # flagged by the app
        self.assertEqual(self.rt.auth.get_device(kid)["rooms"], [self.kitchen])
        self.assertIn((EVENT_AUTH_CHANGED, {}), self.hass.bus.fired)
        # Nothing to prune on the next read: no event.
        self.hass.bus.fired.clear()
        await self._users()
        self.assertNotIn((EVENT_AUTH_CHANGED, {}), self.hass.bus.fired)


class UserPatchTests(_MemberAccessTestCase):
    async def test_sets_camera_access_and_echoes_it(self) -> None:
        device_id = H.enroll(self.rt.auth, role="user", rooms=[self.kitchen])
        status, body = await self._patch(device_id, {"camera_access": "none"})
        self.assertEqual(status, 200)
        self.assertEqual(body["camera_access"], "none")
        self.assertEqual(body["rooms"], [self.kitchen])
        self.assertEqual(self.rt.auth.get_device(device_id)["camera_access"], "none")
        status, body = await self._patch(
            device_id, {"camera_access": "all", "rooms": [self.garage]}
        )
        self.assertEqual(status, 200)
        self.assertEqual((body["camera_access"], body["rooms"]), ("all", [self.garage]))

    async def test_camera_access_alone_is_a_change(self) -> None:
        device_id = H.enroll(self.rt.auth, role="user")
        status, _ = await self._patch(device_id, {})
        self.assertEqual(status, 400)
        status, _ = await self._patch(device_id, {"camera_access": "rooms"})
        self.assertEqual(status, 200)

    async def test_unknown_camera_access_is_400(self) -> None:
        device_id = H.enroll(self.rt.auth, role="user")
        for bad in ("everything", "", 1, None):
            with self.subTest(bad=bad):
                status, body = await self._patch(device_id, {"camera_access": bad})
                self.assertEqual(status, 400, body)
        self.assertEqual(self.rt.auth.get_device(device_id)["camera_access"], "all")

    async def test_a_room_that_does_not_exist_is_400(self) -> None:
        device_id = H.enroll(self.rt.auth, role="user")
        status, body = await self._patch(
            device_id, {"rooms": [self.kitchen, "room-ghost"]}
        )
        self.assertEqual(status, 400)
        self.assertIn("room-ghost", body["message"])
        self.assertIsNone(self.rt.auth.get_device(device_id)["rooms"])
        # Clearing the scope and real rooms still work.
        status, _ = await self._patch(device_id, {"rooms": [self.kitchen]})
        self.assertEqual(status, 200)
        status, body = await self._patch(device_id, {"rooms": None})
        self.assertEqual(status, 200)
        self.assertIsNone(body["rooms"])

    async def test_the_admin_cannot_be_edited(self) -> None:
        owner = H.enroll(self.rt.auth, role="admin")
        status, _ = await self._patch(owner, {"camera_access": "none"})
        self.assertEqual(status, 403)
        self.assertEqual(self.rt.auth.get_device(owner)["camera_access"], "all")

    async def test_only_the_admin_may_edit(self) -> None:
        target = H.enroll(self.rt.auth, role="user")
        _, sub_admin = H.session(self.rt.auth, role="sub-admin")
        resp = await CasaSmartUserView(self.hass).patch(
            H.FakeRequest(headers=sub_admin, body={"camera_access": "none"}), target
        )
        self.assertEqual(resp.status, 403)


class PairingCodeTests(_MemberAccessTestCase):
    async def test_camera_access_is_baked_into_the_code(self) -> None:
        status, body = await self._mint(
            {"role": "user", "rooms": [self.kitchen], "camera_access": "none"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["camera_access"], "none")
        listed = self.rt.pairing.list_codes()[0]
        self.assertEqual(listed["camera_access"], "none")

    async def test_omitted_access_follows_the_rooms(self) -> None:
        status, body = await self._mint({"role": "user"})
        self.assertEqual(status, 201)
        self.assertIsNone(body["camera_access"])

    async def test_unknown_access_or_room_is_400(self) -> None:
        status, _ = await self._mint({"role": "user", "camera_access": "some"})
        self.assertEqual(status, 400)
        status, body = await self._mint({"role": "user", "rooms": ["room-ghost"]})
        self.assertEqual(status, 400)
        self.assertIn("room-ghost", body["message"])
        self.assertEqual(self.rt.pairing.list_codes(), [])

    async def test_add_a_device_copies_the_members_access(self) -> None:
        member = H.enroll(
            self.rt.auth,
            role="user",
            rooms=[self.garage],
            camera_access="none",
            member_id="mem-sara",
        )
        # Whatever the payload says, the member's own grant wins.
        status, body = await self._mint(
            {
                "role": "sub-admin",
                "camera_access": "all",
                "member_id": "mem-sara",
            }
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], "user")
        self.assertEqual(body["rooms"], [self.garage])
        self.assertEqual(body["camera_access"], "none")
        self.assertEqual(body["member_id"], self.rt.auth.member_id_for(member))


class EnrollAndSessionTests(_MemberAccessTestCase):
    async def _enroll(self, code, pem, name="Phone"):
        resp = await CasaSmartEnrollView(self.hass).post(
            H.FakeRequest(
                body={"pairing_code": code, "public_key": pem, "name": name},
                remote=LAN_IP,
            )
        )
        return H.read_response(resp)

    async def _login(self, device_id, private_key):
        status, challenge = H.read_response(
            await CasaSmartChallengeView(self.hass).post(
                H.FakeRequest(body={"device_id": device_id}, remote=LAN_IP)
            )
        )
        self.assertEqual(status, 200)
        signature = base64.b64encode(
            private_key.sign(challenge["nonce"].encode(), ec.ECDSA(hashes.SHA256()))
        ).decode()
        return H.read_response(
            await CasaSmartTokenView(self.hass).post(
                H.FakeRequest(
                    body={
                        "device_id": device_id,
                        "challenge_id": challenge["challenge_id"],
                        "signature": signature,
                    },
                    remote=LAN_IP,
                )
            )
        )

    async def test_enroll_stores_the_codes_access_and_the_session_reports_it(
        self,
    ) -> None:
        H.enroll(self.rt.auth, role="admin")  # a claimed hub
        issued = self.rt.pairing.generate_code(
            "user", rooms=[self.kitchen], camera_access="none"
        )
        private_key, pem = _keypair()
        status, body = await self._enroll(issued["code"], pem)
        self.assertEqual(status, 201)
        self.assertEqual(body["camera_access"], "none")
        self.assertEqual(body["rooms"], [self.kitchen])
        device_id = body["device_id"]
        self.assertEqual(self.rt.auth.get_device(device_id)["camera_access"], "none")

        status, token = await self._login(device_id, private_key)
        self.assertEqual(status, 200)
        self.assertEqual(token["camera_access"], "none")
        self.assertEqual(token["rooms"], [self.kitchen])

        status, who = H.read_response(
            await CasaSmartWhoamiView(self.hass).get(
                H.FakeRequest(headers=H.bearer(token["token"]))
            )
        )
        self.assertEqual(status, 200)
        self.assertTrue(who["enrolled"])
        self.assertEqual(who["camera_access"], "none")
        self.assertEqual(who["rooms"], [self.kitchen])

        # A re-pair with the same key answers the effective access too.
        status, again = await self._enroll(issued["code"], pem)
        self.assertEqual(status, 201)
        self.assertEqual(again["device_id"], device_id)
        self.assertEqual(again["camera_access"], "none")

    async def test_a_code_without_access_enrolls_with_the_room_behaviour(
        self,
    ) -> None:
        H.enroll(self.rt.auth, role="admin")
        issued = self.rt.pairing.generate_code("user", rooms=[self.kitchen])
        _, pem = _keypair()
        status, body = await self._enroll(issued["code"], pem)
        self.assertEqual(status, 201)
        self.assertEqual(body["camera_access"], "rooms")
        self.assertIsNone(self.rt.auth._devices[body["device_id"]]["camera_access"])


if __name__ == "__main__":
    unittest.main()
