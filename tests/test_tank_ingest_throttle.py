"""Since 2.3.0: the tank ingest throttle keys on Home Assistant's resolved client
address, never on client-supplied proxy headers."""

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
from casasmart.storage import HubStorage  # noqa: E402
from casasmart.tank import TankEngine  # noqa: E402
from casasmart.throttle import MAX_FAILURES  # noqa: E402

ATTACKER = "198.51.100.7"


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
        self.addCleanup(tank_api._INGEST_THROTTLE.clear, ATTACKER)

    async def _post(self, token: str, remote: str, headers: dict | None = None):
        resp = await self.view.post(
            H.FakeRequest(
                headers=headers or {},
                body={"device_token": token, "voltage": 1.5},
                remote=remote,
            )
        )
        return H.read_response(resp)

    def test_client_ip_ignores_proxy_headers(self) -> None:
        request = H.FakeRequest(
            headers={"CF-Connecting-IP": "203.0.113.1", "X-Forwarded-For": "10.9.9.9"},
            remote=ATTACKER,
        )
        self.assertEqual(tank_api._client_ip(request), ATTACKER)

    async def test_rotating_cf_connecting_ip_does_not_escape_the_throttle(self) -> None:
        for attempt in range(MAX_FAILURES):
            status, _ = await self._post(
                "bad-token", ATTACKER, {"CF-Connecting-IP": f"203.0.113.{attempt + 1}"}
            )
            self.assertEqual(status, 401)
        status, _ = await self._post(
            "bad-token", ATTACKER, {"CF-Connecting-IP": "203.0.113.200"}
        )
        self.assertEqual(status, 429)

    async def test_valid_reading_is_accepted(self) -> None:
        _, token = self.tanks.mint_device("dev-1", "Tank", "192.168.1.59")
        status, body = await self._post(token, "192.168.1.59")
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "dev-1")
