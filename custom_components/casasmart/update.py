"""Pure self-update logic (Track B — B5) — no HA imports.

The version math and GitHub-release parsing behind ``update_api.py``,
kept import-free so the unit tests run without a Home Assistant install
(same split as ``automations.py`` / ``history.py`` / ``entity_bridge.py``).

Two jobs, both pure:

- ``parse_release`` turns the GitHub ``releases/latest`` JSON into a
  ``ReleaseInfo`` (or ``None`` for a draft / malformed payload).
- ``is_newer`` answers "is the released version newer than the one the
  hub is running" with a small, dependency-free semver comparison.

No network, no HA, no global state — the checker in ``update_api.py``
owns the aiohttp fetch + caching and leans on these for the decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A release tag is the version, with an optional leading "v" (v1.2.3).
# We compare only the numeric release part (1.2.3); a pre-release suffix
# (-beta.1) is parsed out and used solely as a tiebreak (see _split).
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+](.+))?$")


@dataclass(frozen=True)
class ReleaseInfo:
    """The fields the app's update UI needs, distilled from a release."""

    version: str
    changelog: str | None
    published_at: str | None
    release_url: str | None


def _split(raw: Any) -> tuple[tuple[int, ...], str | None] | None:
    """Return ``((release ints), prerelease-or-None)`` or None if unparsable.

    "1.2.3"      -> ((1, 2, 3), None)
    "v0.1"       -> ((0, 1), None)
    "1.0.0-beta" -> ((1, 0, 0), "beta")
    "garbage"    -> None
    """
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    return release, match.group(2)


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Right-pad the shorter tuple with zeros so 1.2 and 1.2.0 compare equal."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def is_newer(current: Any, latest: Any) -> bool:
    """True when ``latest`` is a strictly newer version than ``current``.

    Dependency-free and deliberately small. Rules:

    - Compare the numeric release parts left-to-right (1.2.0 vs 1.10.0).
    - Equal release parts: a final release beats a pre-release of the
      same base (1.0.0 > 1.0.0-rc1), and two pre-releases compare by
      their suffix string. This is enough for the hub's own tags; it is
      NOT a full semver engine and never claims to be.
    - An unparsable ``latest`` is never "newer" (we don't offer a junk
      tag as an update); an unparsable ``current`` is treated as oldest
      so any real release shows as available.
    """
    parsed_latest = _split(latest)
    if parsed_latest is None:
        return False
    parsed_current = _split(current)
    if parsed_current is None:
        return True

    cur_release, cur_pre = parsed_current
    lat_release, lat_pre = parsed_latest
    cur_release, lat_release = _pad(cur_release, lat_release)
    if lat_release != cur_release:
        return lat_release > cur_release

    # Same numeric base — settle on the pre-release suffix.
    if cur_pre == lat_pre:
        return False
    if lat_pre is None:  # final release beats any pre-release of the same base
        return True
    if cur_pre is None:  # latest is a pre-release of a base we already run
        return False
    return lat_pre > cur_pre


def parse_release(payload: Any) -> ReleaseInfo | None:
    """Distill GitHub's ``releases/latest`` JSON into a ``ReleaseInfo``.

    Returns ``None`` for anything we shouldn't offer as an update: a
    non-object payload, a draft, or a release with no usable tag. A
    pre-release IS kept here — whether it counts as "newer" is decided
    later by ``is_newer`` against the running version.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("draft") is True:
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None

    body = payload.get("body")
    published = payload.get("published_at")
    url = payload.get("html_url")
    return ReleaseInfo(
        version=tag.strip(),
        changelog=body.strip() if isinstance(body, str) and body.strip() else None,
        published_at=published if isinstance(published, str) else None,
        release_url=url if isinstance(url, str) else None,
    )
