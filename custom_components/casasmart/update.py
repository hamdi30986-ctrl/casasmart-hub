"""CasaSmart runtime component."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any




_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+](.+))?$")


@dataclass(frozen=True)
class ReleaseInfo:
    """CasaSmart runtime component."""

    version: str
    changelog: str | None
    published_at: str | None
    release_url: str | None
    download_url: str | None


def _split(raw: Any) -> tuple[tuple[int, ...], str | None] | None:
    """CasaSmart runtime component."""
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    return release, match.group(2)


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """CasaSmart runtime component."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def is_newer(current: Any, latest: Any) -> bool:
    """CasaSmart runtime component."""
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


    if cur_pre == lat_pre:
        return False
    if lat_pre is None:
        return True
    if cur_pre is None:
        return False
    return lat_pre > cur_pre


def parse_release(payload: Any) -> ReleaseInfo | None:
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
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









class InstallError(Exception):
    """CasaSmart runtime component."""


def locate_integration_dir(extracted_root: Any, domain: str) -> Path | None:
    """CasaSmart runtime component."""
    root = Path(extracted_root)
    matches = sorted(
        root.rglob(f"custom_components/{domain}/manifest.json"),
        key=lambda p: len(p.parts),
    )
    return matches[0].parent if matches else None


def read_manifest_version(integration_dir: Any) -> str | None:
    """CasaSmart runtime component."""
    manifest = Path(integration_dir) / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def versions_match(tag: Any, manifest_version: Any) -> bool:
    """CasaSmart runtime component."""
    a = _split(tag)
    b = _split(manifest_version)
    if a is None or b is None:
        return False
    a_release, a_pre = a
    b_release, b_pre = b
    a_release, b_release = _pad(a_release, b_release)
    return a_release == b_release and a_pre == b_pre


def swap_integration_dir(current_dir: Any, new_source_dir: Any) -> Path:
    """CasaSmart runtime component."""
    current = Path(current_dir)
    new_source = Path(new_source_dir)
    if not new_source.is_dir():
        raise InstallError(f"replacement source is not a directory: {new_source}")

    backup = current.with_name(current.name + ".bak")
    if backup.exists():
        shutil.rmtree(backup)

    os.rename(current, backup)
    try:
        shutil.copytree(new_source, current)
    except OSError as err:
        if current.exists():
            shutil.rmtree(current, ignore_errors=True)
        os.rename(backup, current)
        raise InstallError(f"failed to install new integration tree: {err}") from err
    return backup
