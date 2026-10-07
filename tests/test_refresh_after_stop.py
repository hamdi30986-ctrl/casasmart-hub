"""A periodic refresh that outlives the entry must not bring anything back.

Cancelling a timer stops future ticks, not one already running: a daily TLS
check reading a re-minted leaf, or an mDNS refresh looking up the address,
can finish after unload. It used to re-open the TLS port (so the next setup
couldn't bind) or re-publish the mDNS record. Real listener and zeroconf
ServiceInfo, fake zeroconf instance. Needs a real Home Assistant.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    import homeassistant.components.zeroconf  # noqa: F401
    from casasmart import discovery, tls

    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class TlsRefreshAfterStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.material = tls.ensure_tls_material(Path(self._tmp.name))
        self.port = _free_port()
        hass = types.SimpleNamespace(
            async_add_executor_job=lambda func, *args: asyncio.to_thread(func, *args)
        )
        self.server = tls.CasaSmartTlsServer(hass, self.port, self.material)
        self.assertTrue(await self.server.async_start([]))
        self.addAsyncCleanup(self.server.async_stop)

    def _rotated(self):
        return dataclasses.replace(self.material, leaf_rotated=True)

    async def test_a_check_finishing_after_stop_leaves_the_port_closed(self) -> None:
        await self.server.async_stop()
        await self.server.async_refresh(self._rotated(), [])
        self.assertTrue(_port_is_free(self.port))

    async def test_stopping_during_a_refresh_leaves_the_port_closed(self) -> None:
        refresh = asyncio.create_task(self.server.async_refresh(self._rotated(), []))
        await asyncio.sleep(0)
        await self.server.async_stop()
        await refresh
        self.assertTrue(_port_is_free(self.port))


class _Zeroconf:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    async def async_register_service(self, info, **kwargs) -> None:
        self.registered[info.name] = info

    async def async_update_service(self, info) -> None:
        self.registered[info.name] = info

    async def async_unregister_service(self, info) -> None:
        self.registered.pop(info.name, None)


@unittest.skipIf(_SKIP, f"Home Assistant unavailable: {_SKIP}")
class MdnsRefreshAfterStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.zc = _Zeroconf()
        patcher = mock.patch(
            "homeassistant.components.zeroconf.async_get_async_instance",
            mock.AsyncMock(return_value=self.zc),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adv = discovery.MdnsAdvertiser(
            object(), hub_id="ab" * 32, hub_name="Hub", api_version=1, port=8443
        )
        self.ip = "192.168.1.10"
        self.adv._async_source_ip = self._source_ip
        await self.adv.async_start()
        self.assertEqual(len(self.zc.registered), 1)

    async def _source_ip(self) -> str:
        return self.ip

    async def test_a_refresh_after_stop_publishes_nothing(self) -> None:
        await self.adv.async_stop()
        self.ip = "192.168.1.11"  # DHCP moved the hub meanwhile
        await self.adv.async_refresh()
        self.assertEqual(self.zc.registered, {})

    async def test_stopping_during_a_refresh_publishes_nothing(self) -> None:
        lookup = asyncio.Event()

        async def _slow_source_ip() -> str:
            await lookup.wait()
            return "192.168.1.11"

        self.adv._async_source_ip = _slow_source_ip
        refresh = asyncio.create_task(self.adv.async_refresh())
        await asyncio.sleep(0)
        stop = asyncio.create_task(self.adv.async_stop())
        await asyncio.sleep(0)
        lookup.set()
        await asyncio.gather(refresh, stop)
        self.assertEqual(self.zc.registered, {})


if __name__ == "__main__":
    unittest.main()
