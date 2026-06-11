"""Tests for tunnel.py — the B7 tunnel-URL validation (pure, stdlib).

The handshake's tunnel block itself is verified live (curl against the
dev hub with/without the config key); this covers the fail-closed
validation matrix that decides what may ever be advertised to phones.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from tunnel import (  # noqa: E402
    TUNNEL_URL_CONFIG_KEY,
    normalize_tunnel_url,
)


class TestNormalizeTunnelUrl(unittest.TestCase):
    def test_plain_https_origin_passes_through(self) -> None:
        self.assertEqual(
            normalize_tunnel_url("https://noha.casasmart.sa"),
            "https://noha.casasmart.sa",
        )

    def test_trailing_slash_normalized(self) -> None:
        self.assertEqual(
            normalize_tunnel_url("https://noha.casasmart.sa/"),
            "https://noha.casasmart.sa",
        )

    def test_surrounding_whitespace_stripped(self) -> None:
        self.assertEqual(
            normalize_tunnel_url("  https://noha.casasmart.sa \n"),
            "https://noha.casasmart.sa",
        )

    def test_path_prefix_kept(self) -> None:
        # A path-mounted tunnel (one hostname, routed prefix) is legal;
        # the app concatenates /api/casasmart/... below it.
        self.assertEqual(
            normalize_tunnel_url("https://edge.casasmart.sa/noha/"),
            "https://edge.casasmart.sa/noha",
        )

    def test_explicit_port_kept(self) -> None:
        self.assertEqual(
            normalize_tunnel_url("https://noha.casasmart.sa:8443"),
            "https://noha.casasmart.sa:8443",
        )

    def test_http_rejected(self) -> None:
        # Bearer tokens ride this URL — plaintext must never be advertised.
        self.assertIsNone(normalize_tunnel_url("http://noha.casasmart.sa"))

    def test_other_schemes_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url("wss://noha.casasmart.sa"))
        self.assertIsNone(normalize_tunnel_url("ftp://noha.casasmart.sa"))

    def test_scheme_relative_and_bare_host_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url("//noha.casasmart.sa"))
        self.assertIsNone(normalize_tunnel_url("noha.casasmart.sa"))

    def test_missing_host_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url("https://"))
        self.assertIsNone(normalize_tunnel_url("https:///path"))

    def test_userinfo_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url("https://user:pw@host.example"))
        self.assertIsNone(normalize_tunnel_url("https://user@host.example"))

    def test_query_and_fragment_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url("https://host.example?x=1"))
        self.assertIsNone(normalize_tunnel_url("https://host.example#frag"))

    def test_empty_query_and_fragment_delimiters_rejected(self) -> None:
        # AUDIT regression: urlsplit reports `https://host?` as an EMPTY
        # (falsy) query — the delimiter itself must be the gate, or a
        # degenerate URL gets advertised that the app rejects.
        self.assertIsNone(normalize_tunnel_url("https://host.example?"))
        self.assertIsNone(normalize_tunnel_url("https://host.example#"))

    def test_non_string_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url(None))
        self.assertIsNone(normalize_tunnel_url(8443))
        self.assertIsNone(normalize_tunnel_url(["https://host.example"]))
        self.assertIsNone(normalize_tunnel_url({"url": "https://host.example"}))

    def test_empty_and_whitespace_rejected(self) -> None:
        self.assertIsNone(normalize_tunnel_url(""))
        self.assertIsNone(normalize_tunnel_url("   "))

    def test_invalid_url_rejected(self) -> None:
        # urlsplit raises ValueError on this (invalid IPv6 literal).
        self.assertIsNone(normalize_tunnel_url("https://[::1"))

    def test_config_key_is_the_documented_one(self) -> None:
        self.assertEqual(TUNNEL_URL_CONFIG_KEY, "tunnel_url")


if __name__ == "__main__":
    unittest.main()
