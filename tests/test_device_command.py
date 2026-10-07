"""View-layer tests for the single-device command endpoint in ``api``.

Runs where Home Assistant is importable (the view harness needs it).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hastubs import install_casasmart_package, install_homeassistant_stubs

# Install the stubs (a no-op where a real Home Assistant is importable) before
# the harness imports the package.
install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402

try:
    from casasmart.api import CasaSmartCommandView

    _ERR = None
except Exception as err:
    CasaSmartCommandView = None
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


@unittest.skipIf(_SKIP, f"casasmart views unimportable: {_SKIP}")
class CommandRecencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass.states.add("light.lamp", state="on")
        # Answer the view's wait for state_changed at once, so the test
        # doesn't sit out its 2 s timeout.
        listeners: list = []

        def _listen(_event_type, handler):
            listeners.append(handler)
            return lambda: listeners.remove(handler)

        async def _call(domain, service, data, *, blocking=False):
            self.hass.services.calls.append((domain, service, data, blocking))
            for handler in list(listeners):
                handler(types.SimpleNamespace(data={"entity_id": data["entity_id"]}))

        self.hass.bus.async_listen = _listen
        self.hass.services.async_call = _call
        for name, value in (
            ("is_served", lambda hass, eid: True),
            ("in_scope", lambda hass, eid, rooms: True),
            ("serialize_device", lambda hass, state: {"entity_id": state.entity_id}),
        ):
            patcher = mock.patch(f"casasmart.api.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recorded: list[tuple[str, str]] = []
        self.rt.now_data = types.SimpleNamespace(
            record_successful_control=lambda member, eid: self.recorded.append(
                (member, eid)
            )
        )
        _, self.headers = H.session(self.rt.auth, role="admin", member_id="mem-a")

    async def _turn_off(self):
        resp = await CasaSmartCommandView(self.hass).post(
            H.FakeRequest(headers=self.headers, body={"action": "turn_off"}),
            "light.lamp",
        )
        return H.read_response(resp)

    async def test_recency_is_recorded_for_the_member(self) -> None:
        status, body = await self._turn_off()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.recorded, [("mem-a", "light.lamp")])

    async def test_failed_member_lookup_keeps_the_successful_result(self) -> None:
        # The light already switched; a storage error while resolving who
        # sent it for the recency list must not turn that into an error.
        def _unavailable(_device_id):
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(self.rt.auth, "member_id_for", _unavailable):
            status, body = await self._turn_off()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            [(d, s) for d, s, _data, _blocking in self.hass.services.calls],
            [("light", "turn_off")],
        )
        self.assertEqual(self.recorded, [])


if __name__ == "__main__":
    unittest.main()
