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

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A release tag is the version, with an optional leading "v" (v1.2.3).
# We compare only the numeric release part (1.2.3); a pre-release suffix
# (-beta.1) is parsed out and used solely as a tiebreak (see _split).
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+](.+))?$")


@dataclass(frozen=True)
class ReleaseInfo:
    """The fields the app's update UI needs, distilled from a release.

    ``download_url`` is the artifact the installer (Piece 3) pulls: a
    ``.zip`` release asset if the release ships one, otherwise GitHub's
    source ``zipball_url``. ``None`` only if the payload has neither.
    """

    version: str
    changelog: str | None
    published_at: str | None
    release_url: str | None
    download_url: str | None


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
        download_url=_pick_download_url(payload),
    )


def _pick_download_url(payload: dict) -> str | None:
    """The best artifact to install from: a ``.zip`` asset, else the zipball.

    A release that ships a packaged integration ``.zip`` is preferred (it
    holds just what the hub needs); failing that we fall back to GitHub's
    auto-generated source ``zipball_url``. ``None`` if neither is present.
    """
    assets = payload.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            href = asset.get("browser_download_url")
            if (
                isinstance(name, str)
                and name.lower().endswith(".zip")
                and isinstance(href, str)
                and href.strip()
            ):
                return href.strip()
    zipball = payload.get("zipball_url")
    return zipball.strip() if isinstance(zipball, str) and zipball.strip() else None


# --- Piece 3: install-side filesystem logic (pure, no HA / no network) -------
#
# The installer in update_install.py owns the aiohttp download + the HA
# restart; everything that touches only the filesystem lives here so it can
# be unit-tested with temp dirs (same pure/IO split as the version math).


class InstallError(Exception):
    """A self-update step failed in a way the caller should surface verbatim."""


def locate_integration_dir(extracted_root: Any, domain: str) -> Path | None:
    """Find ``custom_components/<domain>`` (with a manifest) in an extracted release.

    A GitHub source zipball extracts under a single top-level folder
    (``owner-repo-<sha>/``), so the integration sits some levels deep. A
    packaged asset may put it at the root. We search for the manifest and
    take the shallowest hit, ignoring any nested test fixtures.
    """
    root = Path(extracted_root)
    matches = sorted(
        root.rglob(f"custom_components/{domain}/manifest.json"),
        key=lambda p: len(p.parts),
    )
    return matches[0].parent if matches else None


def read_manifest_version(integration_dir: Any) -> str | None:
    """Read ``version`` from an integration dir's ``manifest.json``, or None."""
    manifest = Path(integration_dir) / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def versions_match(tag: Any, manifest_version: Any) -> bool:
    """True if a release tag and a manifest version are the same release.

    Tolerates a leading ``v`` and trailing-zero differences (``v1.2`` ==
    ``1.2.0``) but requires an exact numeric+prerelease match — a guard
    against installing a payload whose code doesn't match the tag we
    fetched.
    """
    a = _split(tag)
    b = _split(manifest_version)
    if a is None or b is None:
        return False
    a_release, a_pre = a
    b_release, b_pre = b
    a_release, b_release = _pad(a_release, b_release)
    return a_release == b_release and a_pre == b_pre


def swap_integration_dir(current_dir: Any, new_source_dir: Any) -> Path:
    """Atomically replace ``current_dir`` with ``new_source_dir``; return the backup.

    The live integration dir is moved aside to ``<name>.bak`` (an atomic
    same-filesystem rename), then the new tree is copied into place. If the
    copy fails partway, the partial dir is removed and the backup restored
    so the hub is never left without its integration. The caller is
    responsible for pruning the returned backup once the restart succeeds.
    """
    current = Path(current_dir)
    new_source = Path(new_source_dir)
    if not new_source.is_dir():
        raise InstallError(f"replacement source is not a directory: {new_source}")

    backup = current.with_name(current.name + ".bak")
    if backup.exists():
        shutil.rmtree(backup)

    os.rename(current, backup)  # atomic on the same filesystem
    try:
        shutil.copytree(new_source, current)
    except OSError as err:
        if current.exists():
            shutil.rmtree(current, ignore_errors=True)
        os.rename(backup, current)  # roll back to the known-good tree
        raise InstallError(f"failed to install new integration tree: {err}") from err
    return backup
