"""Remote-access tunnel formalization (Track B — B7).

The hub itself never dials Cloudflare — ``cloudflared`` runs as its own
service on the box, configured at client onboarding (plan B7: one-time
install, per-client subdomain, ingress routed at the CasaSmart TLS port
only — never bare HA). What the integration formalizes is the *contract*:
the installer records the hub's public tunnel URL in ``hub_config.json``
(key ``tunnel_url``), and the handshake advertises it so the app captures
the remote path AT PAIRING — exactly like the B10 TLS pin. No Supabase
``hubs`` table, no manual URL entry on the phone.

This module is the pure validation half (stdlib only, flat-importable by
the unit tests like ``discovery.py``/``ws_protocol.py``): one function
that decides whether a configured value is a publishable tunnel URL.

Validation doctrine — **fail closed, never publish garbage**:

- ``https`` only. The app sends its bearer token on this URL; a plaintext
  ``http://`` tunnel URL handed to phones would carry credentials in the
  clear, so it is rejected here rather than trusted downstream (the app
  side independently refuses non-https too — defense in both layers).
- Host required, no userinfo/query/fragment — a tunnel URL is an origin
  (plus optional path prefix), not an arbitrary link. Anything else is a
  config typo and gets logged + dropped, not "cleaned up" silently.
- Trailing slash normalized away so the app's path concatenation
  (``{base}/api/casasmart/...``) can't produce double slashes.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# hub_config.json key the installer sets at onboarding (plan B7).
TUNNEL_URL_CONFIG_KEY = "tunnel_url"


def normalize_tunnel_url(value: object) -> str | None:
    """Return the publishable tunnel URL, or None if unusable.

    None means "don't advertise a tunnel" — the caller logs the reason;
    a misconfigured URL must degrade to LAN-only, never reach a phone.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    if parts.scheme != "https":
        return None
    if not parts.hostname:
        return None
    # An origin (+ optional path prefix), nothing else. Userinfo in
    # particular must never survive into something phones dial.
    if parts.username is not None or parts.password is not None:
        return None
    # Presence, not emptiness — `https://host?` has an EMPTY query, which
    # urlsplit reports as falsy; checking the delimiters themselves keeps
    # a degenerate `?`/`#` from ever being advertised (audit finding).
    if "?" in candidate or "#" in candidate:
        return None

    return candidate.rstrip("/")
