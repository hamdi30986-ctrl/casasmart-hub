"""The TLS listener's port at setup: a bad ``tls_port`` must not stop the hub.

``tls_port`` is a hand-edited hub_config setting. Anything but a whole number
from 1 to 65535 falls back to the default port with a WARNING that names the
bad value, so the listener and mDNS (which refuses an unusable port) both
start. The functions under test are the real ones from ``__init__.py``; the
TLS server, its material and ``hass`` are fakes. Needs a real Home Assistant.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    integration = H.import_integration()
    _ERR = None
except Exception as err:
    _ERR = err

FINGERPRINT = "ab" * 32


class _FakeTlsServer:
    """Records the port it was given; always 'binds'."""

    def __init__(self, hass, port, material, *, trusted_lan_ingress=False):
        self.port = port
        self.material = material

    async def async_start(self, views) -> bool:
        return True


class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


@unittest.skipIf(_ERR, f"CasaSmart unimportable: {_ERR}")
class TlsPortTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, tls_port):
        """Run the real TLS then mDNS setup steps; return the runtime data."""
        hub_config = H.FakeHubConfig()
        if tls_port is not None:
            hub_config.set("tls_port", tls_port)
        runtime = types.SimpleNamespace(hub_config=hub_config, tls=None, mdns=None)
        entry = types.SimpleNamespace(
            runtime_data=runtime, async_on_unload=lambda unsubscribe: None
        )
        material = types.SimpleNamespace(identity_fingerprint=FINGERPRINT)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.multiple(
                integration,
                CasaSmartTlsServer=_FakeTlsServer,
                ensure_tls_material=lambda data_dir: material,
                build_views=lambda hass, hub_version: [],
                async_track_time_interval=lambda *args: lambda: None,
                _read_proc_version=lambda: None,
            ),
            mock.patch.object(
                integration.MdnsAdvertiser, "async_start", mock.AsyncMock()
            ),
        ):
            await integration._async_start_tls(_Hass(), entry, Path(tmp), "2.3.0")
            await integration._async_start_mdns(_Hass(), entry)
        return runtime

    async def test_unusable_values_fall_back_to_the_default_with_a_warning(self):
        for bad in (0, -1, 65536, 70000, "8443", 8443.0, True, [8443]):
            with self.subTest(bad=bad):
                with self.assertLogs(integration._LOGGER, level="WARNING") as logs:
                    runtime = await self._start(bad)
                self.assertEqual(runtime.tls.port, integration.TLS_PORT_DEFAULT)
                self.assertEqual(
                    runtime.mdns._descriptor.port, integration.TLS_PORT_DEFAULT
                )
                messages = [record.getMessage() for record in logs.records]
                self.assertTrue(
                    any("tls_port" in m and repr(bad) in m for m in messages),
                    messages,
                )

    async def test_unset_uses_the_default_quietly(self):
        with self.assertNoLogs(integration._LOGGER, level="WARNING"):
            runtime = await self._start(None)
        self.assertEqual(runtime.tls.port, integration.TLS_PORT_DEFAULT)
        self.assertEqual(runtime.mdns._descriptor.port, integration.TLS_PORT_DEFAULT)

    async def test_a_valid_port_is_used(self):
        for port in (1, 9443, 65535):
            with self.subTest(port=port):
                with self.assertNoLogs(integration._LOGGER, level="WARNING"):
                    runtime = await self._start(port)
                self.assertEqual(runtime.tls.port, port)
                self.assertEqual(runtime.mdns._descriptor.port, port)


if __name__ == "__main__":
    unittest.main()
