"""CasaSmart runtime component."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


TUNNEL_URL_CONFIG_KEY = "tunnel_url"




_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")




_CLOUDFLARED_SLUG_SUFFIX = "_cloudflared"



_RUNNING_ADDON_STATES = frozenset({"started", "startup"})


def normalize_tunnel_url(value: object) -> str | None:
    """CasaSmart runtime component."""
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


    if parts.username is not None or parts.password is not None:
        return None



    if "?" in candidate or "#" in candidate:
        return None

    return candidate.rstrip("/")


def normalize_cloudflare_domain(value: object) -> str | None:
    """CasaSmart runtime component."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None



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


    if labels[-1].isdigit():
        return None
    return host


def domain_to_tunnel_url(value: object) -> str | None:
    """CasaSmart runtime component."""
    host = normalize_cloudflare_domain(value)
    if host is None:
        return None
    return normalize_tunnel_url(f"https://{host}")


def pick_cloudflared_slug(addons: object) -> str | None:
    """CasaSmart runtime component."""
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








_EDGE_ORIGIN_DOWN_STATUSES = frozenset({521, 522, 523, 530})


EDGE_RESTART_COOLDOWN_SECONDS = 900.0


def is_edge_origin_down(status: int) -> bool:
    """CasaSmart runtime component."""
    return status in _EDGE_ORIGIN_DOWN_STATUSES


def edge_watchdog_decision(
    alive: bool | None,
    last_restart: float | None,
    now: float,
    cooldown: float = EDGE_RESTART_COOLDOWN_SECONDS,
) -> str:
    """CasaSmart runtime component."""
    if alive is None:
        return "inconclusive"
    if alive:
        return "up"
    if last_restart is not None and (now - last_restart) < cooldown:
        return "cooldown"
    return "restart"
