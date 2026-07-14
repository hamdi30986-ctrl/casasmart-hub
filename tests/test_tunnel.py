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
    EDGE_RESTART_COOLDOWN_SECONDS,
    TUNNEL_URL_CONFIG_KEY,
    domain_to_tunnel_url,
    edge_watchdog_decision,
    is_edge_origin_down,
    normalize_cloudflare_domain,
    normalize_tunnel_url,
    pick_cloudflared_slug,
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


class TestNormalizeCloudflareDomain(unittest.TestCase):
    """The config/options-flow domain field — same fail-closed doctrine."""

    def test_bare_domain_passes(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("maher-ha.mazinus.com"),
            "maher-ha.mazinus.com",
        )

    def test_uppercase_lowercased(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("MAHER-HA.Mazinus.COM"),
            "maher-ha.mazinus.com",
        )

    def test_surrounding_whitespace_stripped(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("  maher-ha.mazinus.com \n"),
            "maher-ha.mazinus.com",
        )

    def test_trailing_root_dot_normalized(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("maher-ha.mazinus.com."),
            "maher-ha.mazinus.com",
        )

    def test_pasted_https_url_accepted(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("https://maher-ha.mazinus.com"),
            "maher-ha.mazinus.com",
        )

    def test_pasted_https_url_trailing_slash_accepted(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("https://maher-ha.mazinus.com/"),
            "maher-ha.mazinus.com",
        )

    def test_pasted_url_host_lowercased(self) -> None:
        self.assertEqual(
            normalize_cloudflare_domain("https://MAHER-HA.mazinus.com/"),
            "maher-ha.mazinus.com",
        )

    def test_http_url_rejected(self) -> None:
        # Same doctrine as normalize_tunnel_url: plaintext never publishable.
        self.assertIsNone(normalize_cloudflare_domain("http://maher.mazinus.com"))

    def test_other_schemes_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("wss://maher.mazinus.com"))
        self.assertIsNone(normalize_cloudflare_domain("ftp://maher.mazinus.com"))

    def test_scheme_relative_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("//maher.mazinus.com"))

    def test_url_with_path_rejected(self) -> None:
        # A path prefix can't be expressed as a domain — dropping it silently
        # would change what the value means. Fail closed.
        self.assertIsNone(
            normalize_cloudflare_domain("https://edge.casasmart.sa/noha")
        )

    def test_url_with_port_rejected(self) -> None:
        # Cloudflare tunnel hostnames are public DNS on 443 — a port is a typo.
        self.assertIsNone(
            normalize_cloudflare_domain("https://maher.mazinus.com:8443")
        )

    def test_bare_domain_with_port_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("maher.mazinus.com:8443"))

    def test_userinfo_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("user@maher.mazinus.com"))
        self.assertIsNone(
            normalize_cloudflare_domain("https://user@maher.mazinus.com")
        )
        self.assertIsNone(
            normalize_cloudflare_domain("https://user:pw@maher.mazinus.com")
        )

    def test_query_and_fragment_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("maher.mazinus.com?x=1"))
        self.assertIsNone(normalize_cloudflare_domain("maher.mazinus.com#frag"))
        self.assertIsNone(
            normalize_cloudflare_domain("https://maher.mazinus.com?x=1")
        )

    def test_bare_domain_with_path_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("maher.mazinus.com/path"))

    def test_single_label_rejected(self) -> None:
        # A CF tunnel hostname is a public FQDN — at least two labels.
        self.assertIsNone(normalize_cloudflare_domain("localhost"))
        self.assertIsNone(normalize_cloudflare_domain("https://localhost"))

    def test_ipv4_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("192.168.8.100"))
        self.assertIsNone(normalize_cloudflare_domain("https://192.168.8.100"))

    def test_ipv6_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("[::1]"))
        self.assertIsNone(normalize_cloudflare_domain("https://[2001:db8::1]"))

    def test_bad_label_edges_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("-bad.example"))
        self.assertIsNone(normalize_cloudflare_domain("bad-.example"))
        self.assertIsNone(normalize_cloudflare_domain("bad..example"))

    def test_underscore_rejected(self) -> None:
        # Hostnames (which a tunnel domain is) never contain underscores.
        self.assertIsNone(normalize_cloudflare_domain("bad_host.example"))

    def test_overlong_host_rejected(self) -> None:
        long_host = ".".join(["a" * 60] * 5)  # 304 chars > 253
        self.assertIsNone(normalize_cloudflare_domain(long_host))

    def test_overlong_label_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain(f"{'a' * 64}.example"))

    def test_empty_and_whitespace_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain(""))
        self.assertIsNone(normalize_cloudflare_domain("   "))

    def test_non_string_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain(None))
        self.assertIsNone(normalize_cloudflare_domain(443))
        self.assertIsNone(normalize_cloudflare_domain(["maher.mazinus.com"]))

    def test_garbage_rejected(self) -> None:
        self.assertIsNone(normalize_cloudflare_domain("not a domain at all"))
        self.assertIsNone(normalize_cloudflare_domain("https://[::1"))


class TestDomainToTunnelUrl(unittest.TestCase):
    """Domain -> the advertised URL, re-validated end to end."""

    def test_valid_domain_becomes_https_origin(self) -> None:
        self.assertEqual(
            domain_to_tunnel_url("maher-ha.mazinus.com"),
            "https://maher-ha.mazinus.com",
        )

    def test_pasted_url_normalized_to_origin(self) -> None:
        self.assertEqual(
            domain_to_tunnel_url("https://MAHER-HA.mazinus.com/"),
            "https://maher-ha.mazinus.com",
        )

    def test_invalid_domain_gives_none(self) -> None:
        self.assertIsNone(domain_to_tunnel_url("http://maher.mazinus.com"))
        self.assertIsNone(domain_to_tunnel_url("maher.mazinus.com/path"))
        self.assertIsNone(domain_to_tunnel_url(""))
        self.assertIsNone(domain_to_tunnel_url(None))

    def test_roundtrip_agrees_with_url_validator(self) -> None:
        # The two validators must never disagree about what gets published.
        url = domain_to_tunnel_url("maher-ha.mazinus.com")
        self.assertEqual(normalize_tunnel_url(url), url)


class TestPickCloudflaredSlug(unittest.TestCase):
    """Runtime slug discovery — repo-hash prefixes are never hardcoded."""

    def test_empty_listing_gives_none(self) -> None:
        self.assertIsNone(pick_cloudflared_slug([]))

    def test_non_list_gives_none(self) -> None:
        self.assertIsNone(pick_cloudflared_slug(None))
        self.assertIsNone(pick_cloudflared_slug("9074a9fa_cloudflared"))

    def test_no_match_gives_none(self) -> None:
        addons = [
            ("core_mosquitto", "Mosquitto broker", "started"),
            ("a0d7b954_vscode", "Studio Code Server", "stopped"),
        ]
        self.assertIsNone(pick_cloudflared_slug(addons))

    def test_single_match_any_state(self) -> None:
        addons = [
            ("core_mosquitto", "Mosquitto broker", "started"),
            ("9074a9fa_cloudflared", "Cloudflare Tunnel", "stopped"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "9074a9fa_cloudflared"
        )

    def test_prefers_running_match(self) -> None:
        addons = [
            ("1111aaaa_cloudflared", "Cloudflared A", "stopped"),
            ("9074a9fa_cloudflared", "Cloudflared B", "started"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "9074a9fa_cloudflared"
        )

    def test_startup_counts_as_running(self) -> None:
        addons = [
            ("1111aaaa_cloudflared", "Cloudflared A", "stopped"),
            ("9074a9fa_cloudflared", "Cloudflared B", "startup"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "9074a9fa_cloudflared"
        )

    def test_multiple_running_first_sorted(self) -> None:
        addons = [
            ("zzzz9999_cloudflared", "Cloudflared Z", "started"),
            ("1111aaaa_cloudflared", "Cloudflared A", "started"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "1111aaaa_cloudflared"
        )

    def test_none_running_first_sorted(self) -> None:
        addons = [
            ("zzzz9999_cloudflared", "Cloudflared Z", "stopped"),
            ("1111aaaa_cloudflared", "Cloudflared A", "error"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "1111aaaa_cloudflared"
        )

    def test_local_build_slugs_match(self) -> None:
        self.assertEqual(
            pick_cloudflared_slug([("local_cloudflared", "Local CF", "stopped")]),
            "local_cloudflared",
        )
        self.assertEqual(
            pick_cloudflared_slug([("cloudflared", "Bare CF", "stopped")]),
            "cloudflared",
        )

    def test_suffix_only_prefix_does_not_match(self) -> None:
        # "cloudflared_exporter"-style slugs are a different add-on.
        self.assertIsNone(
            pick_cloudflared_slug(
                [("cloudflared_metrics", "CF metrics", "started")]
            )
        )

    def test_malformed_entries_skipped(self) -> None:
        addons = [
            None,
            ("only-two", "items"),
            (123, "numeric slug", "started"),
            ("9074a9fa_cloudflared", "Cloudflare Tunnel", "stopped"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "9074a9fa_cloudflared"
        )

    def test_non_string_state_treated_as_not_running(self) -> None:
        addons = [
            ("zzzz9999_cloudflared", "Cloudflared Z", None),
            ("1111aaaa_cloudflared", "Cloudflared A", "stopped"),
        ]
        self.assertEqual(
            pick_cloudflared_slug(addons), "1111aaaa_cloudflared"
        )


class TestIsEdgeOriginDown(unittest.TestCase):
    def test_edge_origin_down_statuses(self) -> None:
        for status in (521, 522, 523, 530):
            self.assertTrue(is_edge_origin_down(status), status)

    def test_reachable_origin_statuses_are_alive(self) -> None:
        # Any response that reached the origin — 2xx, redirects, auth gates,
        # even 5xx from OUR own server — means the tunnel is up.
        for status in (200, 204, 301, 401, 403, 404, 500, 502, 503):
            self.assertFalse(is_edge_origin_down(status), status)


class TestEdgeWatchdogDecision(unittest.TestCase):
    def test_no_response_is_inconclusive(self) -> None:
        # None = the hub's own internet is likely down; never restart.
        self.assertEqual(
            edge_watchdog_decision(None, None, 1000.0), "inconclusive"
        )

    def test_alive_is_up(self) -> None:
        self.assertEqual(edge_watchdog_decision(True, None, 1000.0), "up")

    def test_origin_down_never_restarted_is_restart(self) -> None:
        self.assertEqual(edge_watchdog_decision(False, None, 1000.0), "restart")

    def test_origin_down_inside_cooldown_holds(self) -> None:
        # Restarted 100s ago, cooldown 900s -> hold.
        self.assertEqual(
            edge_watchdog_decision(False, 900.0, 1000.0), "cooldown"
        )

    def test_origin_down_past_cooldown_restarts_again(self) -> None:
        # Last restart 1000s ago, past the 900s window -> restart again.
        self.assertEqual(
            edge_watchdog_decision(False, 0.0, 1000.0), "restart"
        )

    def test_cooldown_boundary_is_exclusive(self) -> None:
        # Exactly at the cooldown edge counts as expired (>= cooldown restarts).
        now = 5000.0
        last = now - EDGE_RESTART_COOLDOWN_SECONDS
        self.assertEqual(edge_watchdog_decision(False, last, now), "restart")

    def test_alive_ignores_cooldown(self) -> None:
        # A healthy probe is "up" regardless of restart history.
        self.assertEqual(edge_watchdog_decision(True, 4999.0, 5000.0), "up")


if __name__ == "__main__":
    unittest.main()
