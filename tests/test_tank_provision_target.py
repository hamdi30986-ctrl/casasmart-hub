"""Tank provisioning only dials addresses on the local network.

The provision view makes server-side HTTP requests to the address the caller
supplies, so ``_is_lan_target`` must refuse anything that reaches the hub
itself or leaves the LAN. The real view runs; the auth gate and the HTTP
session are fakes, and the session fails the test if it is ever used.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart import tank_api  # noqa: E402
from casasmart.storage import HubStorage  # noqa: E402
from casasmart.tank import TANK_INGEST_URL_CONFIG_KEY, TankEngine  # noqa: E402

# 0.0.0.0 and :: are "this host": connecting to them reaches the hub's own
# loopback services.
_REFUSED = ("0.0.0.0", "::", "127.0.0.1", "::1", "8.8.8.8", "100.64.0.1", "x")
_ACCEPTED = ("192.168.1.50", "10.0.0.7", "172.16.4.2", "169.254.10.1")


class _NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("the hub dialled a refused address")

    post = get


class IsLanTargetTests(unittest.TestCase):
    def test_refused_addresses(self) -> None:
        for ip in _REFUSED:
            with self.subTest(ip=ip):
                self.assertFalse(tank_api._is_lan_target(ip))

    def test_lan_addresses(self) -> None:
        for ip in _ACCEPTED:
            with self.subTest(ip=ip):
                self.assertTrue(tank_api._is_lan_target(ip))


class ProvisionViewTargetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = HubStorage(db_path=Path(tmp.name) / "hub.db")
        storage.open()
        self.addCleanup(storage.close)
        self.tanks = TankEngine(storage.table("tank_devices"), storage.tank_readings())
        hub_config = H.FakeHubConfig()
        hub_config.set(
            TANK_INGEST_URL_CONFIG_KEY,
            "http://192.168.1.2:8123/api/casasmart/tank/reading",
        )
        hass = H.FakeHass(
            types.SimpleNamespace(tanks=self.tanks, hub_config=hub_config)
        )
        self.view = tank_api.CasaSmartTankProvisionView(hass)
        for patcher in (
            mock.patch.object(
                tank_api,
                "authenticate_request",
                lambda hass, request, permission: ({"sub": "dev-admin"}, None),
            ),
            mock.patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                lambda hass: _NoNetwork(),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_this_host_address_is_refused_before_any_request(self) -> None:
        for ip in ("0.0.0.0", "::"):
            with self.subTest(ip=ip):
                request = H.FakeRequest(body={"ip": ip, "name": "Roof Tank"})
                status, body = H.read_response(await self.view.post(request))
                self.assertEqual(status, 400, body)
                self.assertEqual(body["message"], "ip must be a LAN address")
        self.assertEqual(self.tanks.list_devices(), [])


if __name__ == "__main__":
    unittest.main()
