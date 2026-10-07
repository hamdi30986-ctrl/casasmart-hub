"""Remote access through a Cloudflare tunnel: the pure rules.

cloudflared runs outside the integration (on Home Assistant OS, as the
Cloudflare Tunnel add-on that tunnel_control starts and stops) and carries
remote requests to Home Assistant's HTTP port. The hub's public tunnel URL is
kept in hub_config.json and given to the app at pairing. This module
validates tunnel URLs and domains, matches the add-on slug and makes the
edge watchdog's decision. It is stdlib-only, so unit tests import it directly.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# hub_config.json key of the advertised tunnel URL.
TUNNEL_URL_CONFIG_KEY = "tunnel_url"

# One hostname label: 1-63 chars of [a-z0-9-], no leading or trailing hyphen.
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Add-on slugs are <repo hash>_cloudflared, and the hash differs between
# add-on repositories, so match the suffix.
_CLOUDFLARED_SLUG_SUFFIX = "_cloudflared"
# aiohasupervisor AddonState values that count as running, as plain strings.
RUNNING_ADDON_STATES = frozenset({"started", "startup"})


def normalize_tunnel_url(value: object) -> str | None:
    """The tunnel URL to give phones, or None to advertise no tunnel.

    Phones send their bearer token to this URL, so only an https origin with
    an optional path prefix passes; anything else degrades to LAN-only. The
    result keeps the input's case and path, without a trailing slash.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return None

    # urlsplit checks the port only when .port is read; port 0 can't be dialled.
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    if port == 0:
        return None

    if parts.scheme != "https":
        return None
    if not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    # urlsplit reports an empty query or fragment ("https://host?") as falsy.
    if "?" in candidate or "#" in candidate:
        return None

    return candidate.rstrip("/")


def normalize_cloudflare_domain(value: object) -> str | None:
    """The bare lowercase tunnel hostname, or None if unusable.

    Accepts my-ha.example.com or a pasted https://my-ha.example.com/. A path,
    port, userinfo, query or fragment can't be expressed as a domain, so it is
    refused rather than dropped. The host needs at least two labels and can't
    be an IP literal; a trailing dot is removed.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    # A pasted URL must be a valid tunnel URL and a bare origin.
    if "//" in candidate or ":" in candidate:
        url = normalize_tunnel_url(candidate)
        if url is None:
            return None
        parts = urlsplit(url)
        # normalize_tunnel_url has already read the port, so this can't raise.
        if parts.path or parts.port is not None:
            return None
        host = parts.hostname or ""
    else:
        # A bare domain; urlsplit extracts and lowercases the host.
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
    # An all-digit last label means an IPv4 literal, never a tunnel hostname.
    if labels[-1].isdigit():
        return None
    return host


def domain_to_tunnel_url(value: object) -> str | None:
    """The tunnel URL for a Cloudflare domain, or None for an unusable one.

    The URL also goes through normalize_tunnel_url, so the two validators
    can't disagree.
    """
    host = normalize_cloudflare_domain(value)
    if host is None:
        return None
    return normalize_tunnel_url(f"https://{host}")


def is_cloudflared_slug(slug: object) -> bool:
    """True for a cloudflared add-on slug, whichever repository it came from."""
    return isinstance(slug, str) and (
        slug == "cloudflared" or slug.endswith(_CLOUDFLARED_SLUG_SUFFIX)
    )


def pick_cloudflared_slug(addons: object) -> str | None:
    """The cloudflared slug from [(slug, name, state), ...], or None.

    Matches the slug cloudflared or the _cloudflared suffix. Among several, a
    running one wins, then the first in sort order, so the reconciler always
    targets the same add-on.
    """
    if not isinstance(addons, (list, tuple)):
        return None
    matches: list[tuple[str, str]] = []
    for item in addons:
        try:
            slug, _name, state = item
        except (TypeError, ValueError):
            continue
        if not is_cloudflared_slug(slug):
            continue
        matches.append((slug, state if isinstance(state, str) else ""))
    if not matches:
        return None
    running = sorted(slug for slug, state in matches if state in RUNNING_ADDON_STATES)
    if running:
        return running[0]
    return min(slug for slug, _state in matches)


# --- Edge-liveness watchdog -------------------------------------------------

# Cloudflare's edge answers with these when it can't reach the tunnel: 521 web
# server down, 522 timed out, 523 origin unreachable, 530 (error 1033).
_EDGE_ORIGIN_DOWN_STATUSES = frozenset({521, 522, 523, 530})
# At most one restart per window, so a lasting failure (bad credentials, a
# Cloudflare outage) is logged rather than restarted over and over.
EDGE_RESTART_COOLDOWN_SECONDS = 900.0  # 15 min


def is_edge_origin_down(status: int) -> bool:
    """True when a status from the tunnel URL means the edge can't reach the hub.

    Any other status came through the tunnel, so the tunnel is up.
    """
    return status in _EDGE_ORIGIN_DOWN_STATUSES


def edge_watchdog_decision(
    alive: bool | None,
    last_restart: float | None,
    now: float,
    cooldown: float = EDGE_RESTART_COOLDOWN_SECONDS,
) -> str:
    """The watchdog's verdict: "up", "inconclusive", "cooldown" or "restart".

    alive is True when the probe came back through the tunnel, False when the
    edge reports the tunnel down, and None when nothing answered, which
    usually means the hub's own internet is down. Only False outside the
    cooldown restarts, so a local outage can't cause a restart loop.
    """
    if alive is None:
        return "inconclusive"
    if alive:
        return "up"
    if last_restart is not None and (now - last_restart) < cooldown:
        return "cooldown"
    return "restart"
