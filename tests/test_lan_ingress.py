"""LAN relay ingress (v2.2.0): which listener may vouch for "this client is on
the LAN" — the pure policy, and the marker carried end to end through a real
TLS listener and aiohttp routing."""

from __future__ import annotations

import socket
import ssl
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from casasmart.auth_api import is_lan_request  # noqa: E402
from casasmart.lan_ingress import (  # noqa: E402
    is_docker_desktop_kernel,
    is_recognized_lan_relay_ingress,
    resolve_lan_relay_ingress,
)
from casasmart.tls import CasaSmartTlsServer, ensure_tls_material  # noqa: E402

# Real kernel banners.
DOCKER_DESKTOP = (
    "Linux version 6.12.76-linuxkit (root@buildkitsandbox) (gcc (Alpine 15.2.0) "
    "15.2.0, GNU ld (GNU Binutils) 2.45.1) #1 SMP Sun Mar  8 14:41:59 UTC 2026"
)
WSL2 = "Linux version 6.6.87.2-microsoft-standard-WSL2 (root@abc) (gcc 11.2.0) #1 SMP"
HAOS = "Linux version 6.12.47-haos (builder@buildkitsandbox) (gcc 14.3.0) #1 SMP"
DEBIAN = "Linux version 6.1.0-28-amd64 (debian-kernel@lists.debian.org) #1 SMP"
RASPBERRY_PI = "Linux version 6.6.51+rpt-rpi-v8 (serge@raspberrypi.com) #1 SMP"


class PolicyTests(unittest.TestCase):
    def test_docker_desktop_kernel_detection(self) -> None:
        self.assertTrue(is_docker_desktop_kernel(DOCKER_DESKTOP))
        for banner in (WSL2, HAOS, DEBIAN, RASPBERRY_PI, "", None):
            self.assertFalse(is_docker_desktop_kernel(banner), banner)

    def test_auto_trusts_the_listener_only_on_docker_desktop(self) -> None:
        for setting in (None, "auto"):
            self.assertTrue(resolve_lan_relay_ingress(setting, DOCKER_DESKTOP)[0])
            for banner in (HAOS, DEBIAN, RASPBERRY_PI, WSL2, None):
                self.assertFalse(resolve_lan_relay_ingress(setting, banner)[0])

    def test_explicit_on_and_off_win_over_detection(self) -> None:
        for on in ("on", True):
            self.assertTrue(resolve_lan_relay_ingress(on, HAOS)[0])
        for off in ("off", False):
            self.assertFalse(resolve_lan_relay_ingress(off, DOCKER_DESKTOP)[0])

    def test_unrecognized_value_falls_back_to_auto_and_says_so(self) -> None:
        trusted, reason = resolve_lan_relay_ingress("yes please", DOCKER_DESKTOP)
        self.assertTrue(trusted)
        self.assertIn("unrecognized", reason)
        trusted, _ = resolve_lan_relay_ingress(1, HAOS)
        self.assertFalse(trusted)

    def test_recognized_values(self) -> None:
        # Setup warns about anything else, so a typo is not silently "auto".
        for setting in (None, "auto", "on", "off", True, False):
            self.assertTrue(is_recognized_lan_relay_ingress(setting), setting)
        for setting in ("yes", "On", "true", "", 1, 0, [], {}):
            self.assertFalse(is_recognized_lan_relay_ingress(setting), setting)


class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _LanProbeView:
    """Registers like a HomeAssistantView; answers is_lan_request for the caller."""

    url = "/api/casasmart/test/lan-probe"

    def register(self, hass, app, router) -> None:
        router.add_get(self.url, self.get)

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"lan": is_lan_request(request), "remote": request.remote}
        )


class RealTlsListenerTests(unittest.IsolatedAsyncioTestCase):
    """The marker must survive a real TLS handshake and aiohttp routing.

    The test client connects from 127.0.0.1 — loopback, which the address rule
    refuses — so a trusted listener says LAN and an untrusted one does not.
    """

    async def _probe(self, trusted: bool, headers: dict | None = None) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        material = ensure_tls_material(Path(tmp.name))
        server = CasaSmartTlsServer(_Hass(), 0, material, trusted_lan_ingress=trusted)
        self.assertTrue(await server.async_start([_LanProbeView()]))
        self.addAsyncCleanup(server._runner.cleanup)
        # Port 0 on all interfaces gives each address family its own port.
        port = next(
            sock.getsockname()[1]
            for sock in server._site._server.sockets
            if sock.family == socket.AF_INET
        )
        client_ctx = ssl.create_default_context()
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE  # the app pins the key; not under test
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://127.0.0.1:{port}{_LanProbeView.url}",
                ssl=client_ctx,
                headers=headers or {},
            ) as resp:
                self.assertEqual(resp.status, 200)
                return await resp.json()

    async def test_trusted_listener_vouches_for_lan(self) -> None:
        body = await self._probe(trusted=True)
        self.assertEqual(body["remote"], "127.0.0.1")
        self.assertTrue(body["lan"])

    async def test_untrusted_listener_keeps_the_address_rule(self) -> None:
        self.assertFalse((await self._probe(trusted=False))["lan"])

    async def test_cloudflare_headers_are_never_lan_even_on_trusted_listener(
        self,
    ) -> None:
        body = await self._probe(trusted=True, headers={"CF-Ray": "8c1f2e3d4a5b-AMS"})
        self.assertFalse(body["lan"])
