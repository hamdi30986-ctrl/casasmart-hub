"""A failed Shelly upload must not block a retry of the same tank.

Provisioning mints the tank record and its token before it writes the script to
the Shelly. When that write fails, the hub drops the record it just minted, so
the user's retry is not refused as "already registered" (409). A tank that was
registered before the request is never touched.

The real provision view, ``_fetch_device_info`` / ``_push_script`` and a real
``TankEngine`` over a temp DB run; only the Shelly at the RPC seam and the auth
gate are fakes.
"""

from __future__ import annotations

import re
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
from casasmart.tank import (  # noqa: E402
    TANK_INGEST_URL_CONFIG_KEY,
    TankEngine,
    UnknownTokenError,
)

SHELLY_IP = "192.168.1.50"
SHELLY_ID = "shellyplusuni-aabbcc"


class _Reply:
    """One aiohttp response context; ``timeout`` raises like a lost answer."""

    status = 200

    def __init__(self, body=None, *, timeout: bool = False) -> None:
        self._body = body
        self._timeout = timeout

    async def __aenter__(self):
        if self._timeout:
            raise TimeoutError
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def json(self, content_type=None):
        return self._body


class _FakeShelly:
    """A Gen2 Shelly at the HTTP seam: ``GET /shelly`` plus the Script.* RPCs.

    ``fail`` maps an RPC method to ``"error"`` (the device answers with an RPC
    error), ``"timeout"`` (no answer in time) or ``"malformed"`` (a result
    whose fields have the wrong types). ``before`` runs a hook when a method
    arrives, to interleave other hub activity with the upload.
    """

    def __init__(self) -> None:
        self.fail: dict[str, str] = {}
        self.before: dict[str, object] = {}
        self.scripts: dict[int, dict] = {}
        self._next_id = 1

    def get(self, url, timeout):
        return _Reply({"id": SHELLY_ID, "gen": 2, "model": "SNSN-0043X"})

    def post(self, url, json, timeout):
        method, params = json["method"], json["params"]
        if (hook := self.before.pop(method, None)) is not None:
            hook()
        failure = self.fail.get(method)
        if failure == "timeout":
            return _Reply(timeout=True)
        if failure == "error":
            return _Reply({"id": 1, "error": {"code": -1, "message": "refused"}})
        if failure == "malformed":
            return _Reply({"id": 1, "result": {"scripts": 5, "id": "x"}})
        return _Reply({"id": 1, "result": self._handle(method, params)})

    def _handle(self, method: str, params: dict) -> dict:
        if method == "Script.List":
            return {
                "scripts": [
                    {"id": i, "name": s["name"]} for i, s in self.scripts.items()
                ]
            }
        if method == "Script.Create":
            script_id, self._next_id = self._next_id, self._next_id + 1
            self.scripts[script_id] = {"name": params["name"], "code": ""}
            return {"id": script_id}
        if method == "Script.PutCode":
            script = self.scripts[params["id"]]
            previous = script["code"] if params["append"] else ""
            script["code"] = previous + params["code"]
        elif method == "Script.Delete":
            del self.scripts[params["id"]]
        return {}

    def token(self) -> str:
        """The ingest token baked into the device's one CasaSmart script."""
        (script,) = self.scripts.values()
        return re.search(r'token:"([0-9a-f]+)"', script["code"]).group(1)


class ProvisionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = HubStorage(db_path=Path(tmp.name) / "hub.db")
        storage.open()
        self.addCleanup(storage.close)
        self.devices = storage.table("tank_devices")
        self.tanks = TankEngine(self.devices, storage.tank_readings())
        hub_config = H.FakeHubConfig()
        hub_config.set(
            TANK_INGEST_URL_CONFIG_KEY,
            "http://192.168.1.2:8123/api/casasmart/tank/reading",
        )
        hass = H.FakeHass(
            types.SimpleNamespace(tanks=self.tanks, hub_config=hub_config)
        )
        self.view = tank_api.CasaSmartTankProvisionView(hass)
        self.shelly = _FakeShelly()
        for patcher in (
            mock.patch.object(
                tank_api,
                "authenticate_request",
                lambda hass, request, permission: ({"sub": "dev-admin"}, None),
            ),
            mock.patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                lambda hass: self.shelly,
            ),
            # No first reading arrives in a unit test; don't wait for one.
            mock.patch.object(tank_api, "_FIRST_READING_WAIT", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _provision(self):
        request = H.FakeRequest(body={"ip": SHELLY_IP, "name": "Roof Tank"})
        return H.read_response(await self.view.post(request))

    def _device_ids(self) -> list[str]:
        return [device["device_id"] for device in self.tanks.list_devices()]

    async def test_failed_upload_then_retry_registers_one_tank(self) -> None:
        self.shelly.fail["Script.PutCode"] = "error"
        status, body = await self._provision()
        self.assertEqual(status, 502, body)
        self.assertEqual(self._device_ids(), [])

        del self.shelly.fail["Script.PutCode"]
        status, body = await self._provision()
        self.assertEqual(status, 201, body)
        self.assertEqual(body["device_id"], SHELLY_ID)
        self.assertEqual(self._device_ids(), [SHELLY_ID])
        # The retry replaced the half-written script, and the token the
        # device now carries is the one the hub accepts.
        self.assertEqual(len(self.shelly.scripts), 1)
        self.assertEqual(self.tanks.ingest(self.shelly.token(), 1.5), SHELLY_ID)

    async def test_upload_timeout_behaves_like_a_failure(self) -> None:
        # The device may have stored and started the script before the answer
        # was lost; the record and its token are dropped all the same.
        self.shelly.fail["Script.Start"] = "timeout"
        status, body = await self._provision()
        self.assertEqual(status, 502, body)
        self.assertEqual(self._device_ids(), [])
        first_token = self.shelly.token()
        with self.assertRaises(UnknownTokenError):
            self.tanks.ingest(first_token, 1.5)

        del self.shelly.fail["Script.Start"]
        status, body = await self._provision()
        self.assertEqual(status, 201, body)
        self.assertEqual(self._device_ids(), [SHELLY_ID])
        self.assertNotEqual(self.shelly.token(), first_token)
        self.assertEqual(self.tanks.ingest(self.shelly.token(), 1.5), SHELLY_ID)

    async def test_registered_tank_is_refused_and_left_untouched(self) -> None:
        _, token = self.tanks.mint_device(
            SHELLY_ID, "Roof Tank", SHELLY_IP, "SNSN-0043X"
        )
        before = dict(self.devices.items())
        # Even an upload that would fail never runs: the duplicate is refused
        # first, so nothing of the working tank is deleted.
        self.shelly.fail["Script.PutCode"] = "error"
        status, body = await self._provision()
        self.assertEqual(status, 409, body)
        self.assertEqual(dict(self.devices.items()), before)
        self.assertEqual(self.tanks.ingest(token, 1.5), SHELLY_ID)
        self.assertEqual(self.shelly.scripts, {})

    async def test_failure_keeps_a_record_minted_by_another_request(self) -> None:
        # While this upload runs, the tank is deleted and provisioned again
        # elsewhere. The failing request may only undo its own mint.
        replacement = {}

        def reprovision() -> None:
            self.tanks.delete_device(SHELLY_ID)
            _, replacement["token"] = self.tanks.mint_device(
                SHELLY_ID, "Roof Tank", SHELLY_IP
            )

        self.shelly.before["Script.PutCode"] = reprovision
        self.shelly.fail["Script.PutCode"] = "error"
        status, body = await self._provision()
        self.assertEqual(status, 502, body)
        self.assertEqual(self._device_ids(), [SHELLY_ID])
        self.assertEqual(self.tanks.ingest(replacement["token"], 1.5), SHELLY_ID)

    async def test_malformed_script_list_is_a_502_and_the_retry_works(self) -> None:
        self.shelly.fail["Script.List"] = "malformed"
        status, body = await self._provision()
        self.assertEqual(status, 502, body)
        self.assertEqual(self._device_ids(), [])

        del self.shelly.fail["Script.List"]
        status, body = await self._provision()
        self.assertEqual(status, 201, body)
        self.assertEqual(self.tanks.ingest(self.shelly.token(), 1.5), SHELLY_ID)

    async def test_an_unexpected_upload_error_still_undoes_the_mint(self) -> None:
        # Whatever goes wrong during the upload, this request's mint must not
        # survive it, or the retry is refused as a duplicate (409).
        def explode() -> None:
            raise RuntimeError("device answered something unforeseen")

        self.shelly.before["Script.PutCode"] = explode
        with self.assertRaises(RuntimeError):
            await self._provision()
        self.assertEqual(self._device_ids(), [])

        status, body = await self._provision()
        self.assertEqual(status, 201, body)
        self.assertEqual(self._device_ids(), [SHELLY_ID])


if __name__ == "__main__":
    unittest.main()
