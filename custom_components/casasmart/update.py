"""Self-update logic with no Home Assistant or network code.

Version comparison, GitHub release parsing and the filesystem side of an
install (signature check, finding the integration, the atomic swap), kept
apart so the unit tests run without Home Assistant. update_api and
update_install own the HTTP and Home Assistant side.
"""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A tag is the version with an optional leading "v". A pre-release suffix
# (-beta.1) only breaks ties between equal release numbers.
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+](.+))?$")

# The release asset HACS installs (hacs.json "filename") and its signature.
# The built-in updater installs the same file.
RELEASE_ASSET_NAME = "casasmart.zip"
SIGNATURE_ASSET_NAME = RELEASE_ASSET_NAME + ".sig"


@dataclass(frozen=True)
class ReleaseInfo:
    """The fields the app's update screen needs, taken from a GitHub release.

    download_url is the casasmart.zip asset and signature_url its Ed25519
    signature. Either is None when the release lacks it, and the installer
    then refuses. GitHub's source zipball is unsigned and never used.
    """

    version: str
    changelog: str | None
    published_at: str | None
    release_url: str | None
    download_url: str | None
    signature_url: str | None = None


def _split(raw: Any) -> tuple[tuple[int, ...], str | None] | None:
    """Return ((release numbers), pre-release or None), or None if unparsable.

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


def _pad(
    a: tuple[int, ...], b: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Right-pad the shorter tuple with zeros so 1.2 and 1.2.0 compare equal."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def is_newer(current: Any, latest: Any) -> bool:
    """True when latest is a strictly newer version than current.

    Release numbers compare numerically (1.10.0 > 1.2.0). With equal numbers
    a final release beats a pre-release (1.0.0 > 1.0.0-rc1) and pre-releases
    compare as strings: enough for the hub's own tags, not full semver. An
    unparsable latest is never newer; an unparsable current counts as oldest.
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

    if cur_pre == lat_pre:
        return False
    if lat_pre is None:
        return True
    if cur_pre is None:
        return False
    return lat_pre > cur_pre


def parse_release(payload: Any) -> ReleaseInfo | None:
    """A ReleaseInfo from GitHub's releases/latest JSON, or None.

    None for a non-object payload, a draft or a release without a tag.
    Pre-releases are kept; is_newer decides whether one is offered.
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
        signature_url=_asset_urls(payload).get(SIGNATURE_ASSET_NAME),
    )


def _asset_urls(payload: dict) -> dict[str, str]:
    """{asset name: download URL} for a release's uploaded assets."""
    urls: dict[str, str] = {}
    assets = payload.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            href = asset.get("browser_download_url")
            if isinstance(name, str) and isinstance(href, str) and href.strip():
                urls.setdefault(name, href.strip())
    return urls


def _pick_download_url(payload: dict) -> str | None:
    """The casasmart.zip asset URL, or None (never the source zipball)."""
    return _asset_urls(payload).get(RELEASE_ASSET_NAME)


# --- Install: the filesystem side -------------------------------------------

# Working dirs live in <config>/casasmart/update, not custom_components: HA
# loads any directory there by its manifest, so a copy could replace the live one.
UPDATE_DIR_NAME = "update"
# The previous version, kept after a successful swap.
ROLLBACK_DIR_NAME = "rollback"
# Per-swap work: the download, the staged new tree, trees on their way out.
STAGING_DIR_NAME = "staging"

# Dirs that earlier versions left in custom_components: the rollback, a
# retired rollback and a staged new tree.
_LEGACY_SUFFIX_RE = re.compile(r"\.(?:bak|bak-old-[0-9a-f]{8}|new-[0-9a-f]{8})")


class InstallError(Exception):
    """A self-update step failed; the message goes back to the app as is."""


def verify_release_signature(
    archive: bytes, signature: bytes, public_key_b64: str
) -> None:
    """Raise InstallError unless signature is the release key's signature.

    Plain Ed25519 over the bytes of casasmart.zip, as scripts/release.sh
    signs it.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True)
        )
    except (ValueError, binascii.Error) as err:
        raise InstallError(f"release signing key is unusable: {err}") from err
    try:
        key.verify(signature, archive)
    except InvalidSignature as err:
        raise InstallError("release signature does not verify") from err


def locate_integration_dir(extracted_root: Any, domain: str) -> Path | None:
    """Find the integration dir in an extracted release, or None.

    The release asset has the integration at the zip root (the HACS layout).
    Otherwise the shallowest custom_components/<domain> wins, so nested test
    fixtures are ignored.
    """
    root = Path(extracted_root)
    if _manifest_domain(root / "manifest.json") == domain:
        return root
    matches = sorted(
        root.rglob(f"custom_components/{domain}/manifest.json"),
        key=lambda p: len(p.parts),
    )
    return matches[0].parent if matches else None


def _manifest_domain(manifest: Path) -> str | None:
    """The domain declared in a manifest file, or None if unreadable."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    domain = data.get("domain") if isinstance(data, dict) else None
    return domain if isinstance(domain, str) else None


def read_manifest_version(integration_dir: Any) -> str | None:
    """The version in an integration dir's manifest.json, or None."""
    manifest = Path(integration_dir) / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def versions_match(tag: Any, manifest_version: Any) -> bool:
    """True if a release tag and a manifest version name the same release.

    A leading v and trailing zeros don't matter (v1.2 matches 1.2.0); the
    pre-release suffix must match.
    """
    a = _split(tag)
    b = _split(manifest_version)
    if a is None or b is None:
        return False
    a_release, a_pre = a
    b_release, b_pre = b
    a_release, b_release = _pad(a_release, b_release)
    return a_release == b_release and a_pre == b_pre


def swap_integration_dir(
    current_dir: Any, new_source_dir: Any, update_dir: Any
) -> Path:
    """Replace current_dir with new_source_dir, all or nothing.

    Returns <update_dir>/rollback, which now holds the old live tree. The new
    tree is copied into <update_dir>/staging first, so a failed copy changes
    nothing. Then four renames: live dir to staging, copy to live, previous
    rollback aside, old live tree to rollback. The live dir moves first
    because that rename leaves custom_components: on another filesystem it
    fails with EXDEV before anything has changed. Any failure undoes the
    earlier renames and raises InstallError.
    """
    current = Path(current_dir)
    new_source = Path(new_source_dir)
    if not new_source.is_dir():
        raise InstallError(f"replacement source is not a directory: {new_source}")

    work = Path(update_dir)
    rollback = work / ROLLBACK_DIR_NAME
    staging = work / STAGING_DIR_NAME
    # Unique per swap, so overlapping swaps never share a staging name.
    token = secrets.token_hex(4)
    staged = staging / f"new-{token}"
    outgoing = staging / f"live-{token}"
    retired = staging / f"rollback-{token}"

    try:
        staging.mkdir(parents=True, exist_ok=True)
        shutil.copytree(new_source, staged)
    except OSError as err:
        shutil.rmtree(staged, ignore_errors=True)
        raise InstallError(f"failed to install new integration tree: {err}") from err

    done: list[tuple[Path, Path]] = []  # renames so far, to undo in reverse
    try:
        os.rename(current, outgoing)
        done.append((current, outgoing))
        os.rename(staged, current)
        done.append((staged, current))
        if rollback.exists():
            os.rename(rollback, retired)
            done.append((rollback, retired))
        os.rename(outgoing, rollback)
    except OSError as err:
        if err.errno == errno.EXDEV:
            message = (
                f"cannot update in place: {current.parent} and {work} are on "
                "different filesystems; update through HACS instead"
            )
        else:
            message = f"failed to swap in new integration tree: {err}"
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError as undo_err:
                message += f"; could not move {dst} back to {src}: {undo_err}"
        shutil.rmtree(staged, ignore_errors=True)
        raise InstallError(message) from err
    shutil.rmtree(retired, ignore_errors=True)
    return rollback


@dataclass(frozen=True)
class LegacyDirAction:
    """What ``clear_legacy_update_dirs`` did with one directory."""

    path: Path
    # Where it was moved, or None when it was removed (or not handled).
    moved_to: Path | None = None
    # Why it could not be moved or removed; None on success.
    error: str | None = None


def clear_legacy_update_dirs(
    integration_dir: Any, update_dir: Any
) -> list[LegacyDirAction]:
    """Move or remove the self-update dirs earlier versions left in custom_components.

    <name>.bak becomes the rollback if there is none yet and is removed
    otherwise; <name>.bak-old-<hex> and <name>.new-<hex> are removed. Symlinks
    are not followed. One failure doesn't stop the rest; each outcome is
    returned for the log.
    """
    current = Path(integration_dir)
    rollback = Path(update_dir) / ROLLBACK_DIR_NAME
    actions: list[LegacyDirAction] = []
    for entry in sorted(current.parent.iterdir()):
        if not entry.name.startswith(current.name):
            continue
        suffix = entry.name[len(current.name) :]
        if not _LEGACY_SUFFIX_RE.fullmatch(suffix):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            if suffix == ".bak" and not rollback.exists():
                rollback.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(entry, rollback)
                actions.append(LegacyDirAction(entry, moved_to=rollback))
            else:
                shutil.rmtree(entry)
                actions.append(LegacyDirAction(entry))
        except OSError as err:
            actions.append(LegacyDirAction(entry, error=str(err)))
    return actions
