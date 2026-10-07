"""Live connections when the entry unloads, the hub is factory reset, or a
phone drops mid-request.

aiohttp waits for running handlers when a listener shuts down, and a socket's
handler runs until the socket closes, so an open socket used to hold a reload
for over a minute. Unload now closes every socket with "going away" first. A
factory reset closes them too, so a wiped phone can't keep streaming. A request
whose client goes away keeps running: its handler may be half-way through a
device command.

Runs the real TLS listener, WebSocket view, unload and reset service over a
real aiohttp client; only hass is faked. Needs a real Home Assistant.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    import aiohttp
    from aiohttp import web
    from casasmart import tls
    from casasmart import ws as wsmod
    from homeassistant.components.http import HomeAssistantView
    from homeassistant.exceptions import HomeAssistantError

    integration = H.import_integration()
    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR

# Well inside aiohttp's default shutdown wait (60 s) and the first-frame
# timeout (30 s), either of which would hold a stop that ignores the socket.
_STOP_BUDGET = 3.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Bus:
    def async_listen(self, event_type, handler):
        return lambda: None


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class TlsSocketUnloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hass, self.rt = H.make_hub(self._tmp.name)
        self.addCleanup(self.rt.storage.close)
        self.hass.bus = _Bus()
        self.hass.is_stopping = False

        async def _unload_platforms(entry, platforms):
            return True

        self.hass.config_entries.async_unload_platforms = _unload_platforms

        material = tls.ensure_tls_material(Path(self._tmp.name))
        self.port = _free_port()
        self.server = tls.CasaSmartTlsServer(self.hass, self.port, material)
        await self.server.async_start([wsmod.CasaSmartWebSocketView(self.hass, "t")])
        self.addAsyncCleanup(self.server.async_stop)

        runtime = types.SimpleNamespace(
            storage=self.rt.storage,
            tls=self.server,
            **dict.fromkeys(
                (
                    "suggestions",
                    "energy_controller",
                    "alarm_adapter",
                    "tank_push_monitor",
                    "relay_registrar",
                    "push_dispatcher",
                    "athan_scheduler",
                    "audio_adapter",
                    "mdns",
                )
            ),
        )
        self.entry = types.SimpleNamespace(runtime_data=runtime)

        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    async def _open_socket(self, *, authenticated: bool):
        client = await self.session.ws_connect(
            f"https://127.0.0.1:{self.port}/api/casasmart/ws", ssl=False
        )
        if authenticated:
            device_id, _ = H.session(self.rt.auth, role="admin")
            await client.send_json(
                {"type": "auth", "token": H.token_for(self.rt.auth, device_id)}
            )
            self.assertEqual((await client.receive_json(timeout=2))["type"], "auth_ok")
        return client

    async def _assert_unload_closes(self, client) -> None:
        # The app reads continuously, so it answers the close at once.
        reader = asyncio.create_task(client.receive())
        async with asyncio.timeout(_STOP_BUDGET):
            self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        msg = await asyncio.wait_for(reader, 2)
        self.assertEqual(msg.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(msg.data, aiohttp.WSCloseCode.GOING_AWAY)

    async def test_authenticated_socket_is_closed_and_unload_is_quick(self) -> None:
        await self._assert_unload_closes(await self._open_socket(authenticated=True))

    async def test_socket_awaiting_its_first_frame_is_closed_too(self) -> None:
        await self._assert_unload_closes(await self._open_socket(authenticated=False))

    async def test_factory_reset_closes_sockets_whatever_the_reload_does(
        self,
    ) -> None:
        self.rt.energy_controller = None
        self.rt.energy_flags = types.SimpleNamespace(disabled_automations=lambda: [])
        # A reload that never unloads (it failed, or is still waiting).
        self.hass.config_entries.async_reload = mock.AsyncMock(return_value=False)
        integration._async_register_services(self.hass)
        reset = self.hass.services.handlers[("casasmart", "factory_reset")]

        client = await self._open_socket(authenticated=True)
        reader = asyncio.create_task(client.receive())
        with self.assertRaises(HomeAssistantError):  # the failed reload is reported
            await reset(
                types.SimpleNamespace(
                    data={}, context=types.SimpleNamespace(user_id=None)
                )
            )
        msg = await asyncio.wait_for(reader, 2)
        self.assertEqual(msg.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(msg.data, aiohttp.WSCloseCode.GOING_AWAY)


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class TlsDroppedClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_dropped_connection_does_not_cancel_the_handler(self) -> None:
        # A device command or scene runs a service call under its handler;
        # cancelling it when the phone's connection drops (a Wi-Fi to LTE
        # swap, the app killed) would leave the command half sent.
        class SlowView(HomeAssistantView):
            url = "/api/casasmart/slow"
            name = "api:casasmart:slow"
            requires_auth = False

            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.outcome: list[str] = []

            async def get(self, request):
                self.started.set()
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    self.outcome.append("cancelled")
                    raise
                self.outcome.append("finished")
                return web.Response(text="ok")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hass, rt = H.make_hub(tmp.name)
        self.addCleanup(rt.storage.close)
        hass.is_stopping = False
        port = _free_port()
        server = tls.CasaSmartTlsServer(
            hass, port, tls.ensure_tls_material(Path(tmp.name))
        )
        view = SlowView()
        await server.async_start([view])
        self.addAsyncCleanup(server.async_stop)

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=context)
        writer.write(b"GET /api/casasmart/slow HTTP/1.1\r\nHost: hub\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(view.started.wait(), 2)
        writer.close()  # the phone goes away mid-request
        await writer.wait_closed()
        await asyncio.sleep(0.8)
        self.assertEqual(view.outcome, ["finished"])


if __name__ == "__main__":
    unittest.main()
