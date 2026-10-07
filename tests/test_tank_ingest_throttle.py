"""Since 2.3.0: the tank ingest throttle keys on the client address, which is
the peer unless the request really came through Cloudflare, and a valid token
is never blocked by other senders' failures."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart import tank_api  # noqa: E402
from casasmart.auth_api import client_address  # noqa: E402
from casasmart.storage import HubStorage  # noqa: E402
from casasmart.tank import TankEngine  # noqa: E402
from casasmart.throttle import MAX_FAILURES  # noqa: E402

ATTACKER = "198.51.100.7"
# What cloudflared looks like to the hub: one address for every tunnel client.
TUNNEL = "127.0.0.1"


def _via_cloudflare(client_ip: str) -> dict[str, str]:
    return {"CF-Connecting-IP": client_ip, "CF-Ray": "8a1b2c3d4e5f0000-DXB"}


class ClientAddressTests(unittest.TestCase):
    def test_the_peer_unless_the_request_came_through_cloudflare(self) -> None:
        request = H.FakeRequest(
            headers={"X-Forwarded-For": "10.9.9.9"},
            remote=ATTACKER,
        )
        self.assertEqual(client_address(request), ATTACKER)

    def test_x_forwarded_for_is_never_read(self) -> None:
        request = H.FakeRequest(
            headers={**_via_cloudflare("203.0.113.1"), "X-Forwarded-For": "10.9.9.9"},
            remote=TUNNEL,
        )
        self.assertEqual(client_address(request), "cf:203.0.113.1")

    def test_cloudflare_markers_without_a_client_ip_fall_back_to_the_peer(
        self,
    ) -> None:
        request = H.FakeRequest(
            headers={"CF-Ray": "8a1b2c3d4e5f0000-DXB"}, remote=TUNNEL
        )
        self.assertEqual(client_address(request), TUNNEL)


class IngestThrottleKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = HubStorage(db_path=Path(tmp.name) / "hub.db")
        storage.open()
        self.addCleanup(storage.close)
        self.tanks = TankEngine(storage.table("tank_devices"), storage.tank_readings())
        self.hass = H.FakeHass(types.SimpleNamespace(tanks=self.tanks))
        self.view = tank_api.CasaSmartTankReadingView(self.hass)
        for key in (ATTACKER, TUNNEL, "cf:203.0.113.1", "cf:203.0.113.2"):
            self.addCleanup(tank_api._INGEST_THROTTLE.clear, key)

    async def _post(self, token: str, remote: str, headers: dict | None = None):
        resp = await self.view.post(
            H.FakeRequest(
                headers=headers or {},
                body={"device_token": token, "voltage": 1.5},
                remote=remote,
            )
        )
        return H.read_response(resp)

    async def test_bad_tokens_lock_their_source_out(self) -> None:
        for _ in range(MAX_FAILURES):
            status, _ = await self._post("bad-token", ATTACKER)
            self.assertEqual(status, 401)
        status, _ = await self._post("bad-token", ATTACKER)
        self.assertEqual(status, 429)

    async def test_tunnel_clients_get_their_own_buckets(self) -> None:
        # Every tunnel request reaches the hub from cloudflared's address, so
        # one remote tank with a stale token must not lock the others out.
        for _ in range(MAX_FAILURES):
            status, _ = await self._post(
                "bad-token", TUNNEL, _via_cloudflare("203.0.113.1")
            )
            self.assertEqual(status, 401)
        status, _ = await self._post(
            "bad-token", TUNNEL, _via_cloudflare("203.0.113.1")
        )
        self.assertEqual(status, 429)
        status, _ = await self._post(
            "bad-token", TUNNEL, _via_cloudflare("203.0.113.2")
        )
        self.assertEqual(status, 401)

    async def test_valid_reading_is_accepted(self) -> None:
        _, token = self.tanks.mint_device("dev-1", "Tank", "192.168.1.59")
        status, body = await self._post(token, "192.168.1.59")
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "dev-1")
