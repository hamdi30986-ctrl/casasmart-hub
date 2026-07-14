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

import re
from urllib.parse import urlsplit

# hub_config.json key the installer sets at onboarding (plan B7).
TUNNEL_URL_CONFIG_KEY = "tunnel_url"

# One DNS label: 1-63 chars of [a-z0-9-], no leading/trailing hyphen.
# (Hostnames only — underscores are legal in some DNS records but not in
# hostnames, and a Cloudflare tunnel hostname is a hostname.)
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Add-on slugs are `<repo-hash>_cloudflared` and the hash is NOT stable across
# add-on repos (observed `9074a9fa_cloudflared`; the repo just migrated to
# homeassistant-apps/app-cloudflared, so future installs may differ again).
# Match on the suffix — never a hardcoded slug.
_CLOUDFLARED_SLUG_SUFFIX = "_cloudflared"
# Add-on states that count as "running" when picking among multiple matches
# (mirrors aiohasupervisor AddonState values — kept as strings so this module
# stays stdlib-only and flat-importable by the unit tests).
_RUNNING_ADDON_STATES = frozenset({"started", "startup"})


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


def normalize_cloudflare_domain(value: object) -> str | None:
    """Return the bare lowercase tunnel hostname, or None if unusable.

    The config/options flow field accepts what a human is likely to paste:
    ``maher-ha.mazinus.com`` or a full ``https://maher-ha.mazinus.com/``.
    Anything that is not reducible to a plain FQDN is rejected — same
    fail-closed doctrine as ``normalize_tunnel_url``:

    - A pasted URL must be an https *origin*. A path prefix (``/noha``),
      port, userinfo, query or fragment cannot be expressed as a domain,
      and silently dropping any of them would change what the value means.
    - A Cloudflare tunnel hostname is a public DNS name on 443 — no ports,
      no IP literals, at least two labels.
    - Trailing root dot (``host.example.``) and case are normalized away.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    # Pasted-URL form: validate as a tunnel URL first (https-only, no
    # userinfo/query/fragment), then require it to be a bare origin.
    if "//" in candidate or ":" in candidate:
        url = normalize_tunnel_url(candidate)
        if url is None:
            return None
        parts = urlsplit(url)
        if parts.path:
            return None
        try:
            if parts.port is not None:
                return None
        except ValueError:
            return None
        host = parts.hostname or ""
    else:
        # Bare-domain form. urlsplit does the lowercasing/IDNA-safe host
        # extraction; a "/", "?" or "#" in the value lands in path/query/
        # fragment of the constructed URL and is rejected above or here.
        if "/" in candidate or "?" in candidate or "#" in candidate or "@" in candidate:
            return None
        try:
            host = urlsplit(f"https://{candidate}").hostname or ""
        except ValueError:
            return None

    host = host.rstrip(".")
    if not host or len(host) > 253:
        return None
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if not all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return None
    # An all-digit final label is an IPv4 literal, not a DNS name — a
    # Cloudflare tunnel hostname can never be an IP.
    if labels[-1].isdigit():
        return None
    return host


def domain_to_tunnel_url(value: object) -> str | None:
    """Derive the advertised tunnel URL from a Cloudflare domain.

    ``None`` in, ``None`` out — the caller decides whether that means
    "don't advertise" or "leave the existing URL alone". The result is
    re-run through ``normalize_tunnel_url`` so the two validators can
    never disagree about what gets published to phones.
    """
    host = normalize_cloudflare_domain(value)
    if host is None:
        return None
    return normalize_tunnel_url(f"https://{host}")


def pick_cloudflared_slug(addons: object) -> str | None:
    """Pick the cloudflared add-on slug from a Supervisor add-on listing.

    Input is ``[(slug, name, state), ...]`` (plain strings — the HA-coupled
    controller flattens the aiohasupervisor models so this stays pure and
    unit-testable). Matching is by slug suffix ``_cloudflared`` (repo-hash
    prefixes are not stable) plus the locally-built ``cloudflared``. With
    several matches, prefer a running one, else first sorted — deterministic
    so the reconciler always targets the same add-on; the caller logs the
    choice.
    """
    if not isinstance(addons, (list, tuple)):
        return None
    matches: list[tuple[str, str]] = []
    for item in addons:
        try:
            slug, _name, state = item
        except (TypeError, ValueError):
            continue
        if not isinstance(slug, str) or not slug:
            continue
        if slug != "cloudflared" and not slug.endswith(_CLOUDFLARED_SLUG_SUFFIX):
            continue
        matches.append((slug, state if isinstance(state, str) else ""))
    if not matches:
        return None
    running = sorted(slug for slug, state in matches if state in _RUNNING_ADDON_STATES)
    if running:
        return running[0]
    return min(slug for slug, _state in matches)


# --- Edge-liveness watchdog (Phase 9) ---------------------------------------
# Cloudflare returns these statuses from its EDGE when the edge is reachable but
# it cannot reach the origin tunnel — i.e. cloudflared is running yet its edge
# connection is dead. That is the exact "running but offline" case the add-on's
# own state can't see, and the exact signal to restart. 521 web-server-down,
# 522 conn-timed-out, 523 origin-unreachable, 530 (Argo/1033 tunnel error).
_EDGE_ORIGIN_DOWN_STATUSES = frozenset({521, 522, 523, 530})
# Never restart more than once per this window: a real flap gets one restart; a
# persistent failure (bad creds, Cloudflare outage) is logged, not hammered.
EDGE_RESTART_COOLDOWN_SECONDS = 900.0  # 15 min


def is_edge_origin_down(status: int) -> bool:
    """True when an HTTP status from the tunnel URL means Cloudflare's edge is
    up but the origin tunnel is unreachable (restart cloudflared). Any other
    status means the request reached the origin, so the tunnel is alive."""
    return status in _EDGE_ORIGIN_DOWN_STATUSES


def edge_watchdog_decision(
    alive: bool | None,
    last_restart: float | None,
    now: float,
    cooldown: float = EDGE_RESTART_COOLDOWN_SECONDS,
) -> str:
    """Pure watchdog verdict from a probe result + restart history.

    [alive] is the tri-state from the controller's edge probe:
    True (edge+origin up) / False (edge up, origin down) / None (no response —
    the hub's OWN internet is likely down, NOT the tunnel).

    Returns one of: ``"up"``, ``"inconclusive"``, ``"cooldown"``, ``"restart"``.
    We restart ONLY on a definitive origin-down that's outside the cooldown —
    never on an inconclusive result, so a local-internet outage can't trigger a
    pointless restart loop.
    """
    if alive is None:
        return "inconclusive"
    if alive:
        return "up"
    if last_restart is not None and (now - last_restart) < cooldown:
        return "cooldown"
    return "restart"
