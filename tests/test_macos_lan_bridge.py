"""Tests for the macOS LAN bridge scripts in ``deploy/macos``.

Covers the Bonjour publisher (``mdns_publish.py``) and the TLS relay
(``tls_relay.py``), loaded straight from their files.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MDNS = _load("casasmart_mdns_publish", ROOT / "deploy/macos/mdns_publish.py")
RELAY = _load("casasmart_tls_relay", ROOT / "deploy/macos/tls_relay.py")


class MdnsPublisherTest(unittest.TestCase):
    def test_advertisement_uses_live_handshake_identity(self) -> None:
        fingerprint = "ab" * 32
        advertisement = MDNS.advertisement_from_handshake(
            {
                "api_version": 1,
                "tls": {"identity_fingerprint_sha256": fingerprint},
            },
            address="192.168.1.25",
            advertised_port=8443,
            hub_name="CasaSmart Hub",
        )
        command = MDNS.dns_sd_command(advertisement, "/usr/bin/dns-sd")
        self.assertIn(f"id={fingerprint}", command)
        self.assertIn("api=1", command)
        self.assertIn("192.168.1.25", command)
        self.assertIn("8443", command)

    def test_invalid_handshake_identity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MDNS.advertisement_from_handshake(
                {"api_version": 1, "tls": {"identity_fingerprint_sha256": "bad"}},
                address="192.168.1.25",
                advertised_port=8443,
                hub_name="CasaSmart Hub",
            )


class TlsRelayTest(unittest.IsolatedAsyncioTestCase):
    def test_only_local_address_ranges_are_accepted(self) -> None:
        self.assertTrue(RELAY.is_lan_peer(("192.168.1.25", 12345)))
        self.assertTrue(RELAY.is_lan_peer(("10.0.0.8", 12345)))
        self.assertTrue(RELAY.is_lan_peer(("127.0.0.1", 12345)))
        self.assertTrue(RELAY.is_lan_peer(("fe80::1%en0", 12345, 0, 4)))
        self.assertFalse(RELAY.is_lan_peer(("8.8.8.8", 12345)))
        self.assertFalse(RELAY.is_lan_peer(None))

    async def test_relay_is_byte_for_byte(self) -> None:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            writer.write(await reader.readexactly(8))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        relay = await RELAY.start_relay("127.0.0.1", 0, "127.0.0.1", upstream_port)
        relay_port = relay.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"CasaTest")
        await writer.drain()
        self.assertEqual(await reader.readexactly(8), b"CasaTest")
        writer.close()
        await writer.wait_closed()
        relay.close()
        upstream.close()
        await relay.wait_closed()
        await upstream.wait_closed()


if __name__ == "__main__":
    unittest.main()
