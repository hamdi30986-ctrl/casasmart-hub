"""Self-update execution (Track B — B5, Piece 3) — the install action.

``POST /api/casasmart/update/install`` (owner/admin-only, ``update.install``)
turns the "update available" state Piece 1 reports into an actual upgrade:

    1. Re-check the latest release; refuse (409) if nothing is newer.
    2. Download the release's ``casasmart.zip`` asset (the file HACS
       installs) and its ``casasmart.zip.sig``. No signature, no install —
       the source zipball is never used.
    3. Verify the Ed25519 signature against the pinned release key
       (``const.UPDATE_SIGNING_PUBLIC_KEY_B64``) BEFORE extracting anything.
    4. Extract it (every member must stay inside the staging dir), locate
       the integration, and verify its ``manifest.json`` version matches
       the tag we fetched.
    5. Atomically swap the live integration dir for the new tree, keeping a
       ``.bak`` rollback.
    6. Schedule an HA restart *after* the HTTP response flushes, so the app
       gets a clean "installing" reply before the connection drops; the
       container's restart policy brings HA back on the new code.

A hub managed by HACS should be updated through HACS: a self-update swaps
the files without HACS knowing, so HACS keeps reporting the old version.

The filesystem mechanics (locate / version-match / atomic swap) are pure and
live in ``update.py`` so they unit-test with temp dirs. This module owns the
network download, the zip extraction, and the HA restart — the parts that
need a running hub and are proven live rather than in unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import zipfile
from http import HTTPStatus
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant

from .const import DOMAIN, UPDATE_SIGNING_PUBLIC_KEY_B64
from .update import (
    InstallError,
    locate_integration_dir,
    read_manifest_version,
    swap_integration_dir,
    verify_release_signature,
    versions_match,
)
from .update_api import UpdateChecker

_LOGGER = logging.getLogger(__name__)

# Downloading a release tarball is heavier than the status poll — give it
# room, but still bounded so a wedged transfer can't hang forever.
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
# ``browser_download_url`` asset downloads ignore Accept; a GitHub media type is
# kept so the request looks like every other GitHub API call the hub makes.
_DOWNLOAD_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "CasaSmart-Hub",
}
# An Ed25519 signature is 64 bytes; anything much larger is not one.
_MAX_SIGNATURE_BYTES = 1024
# Seconds to let the HTTP response flush to the app before we pull the rug.
_RESTART_GRACE_SECONDS = 2.0


def _integration_dir() -> Path:
    """The live ``custom_components/casasmart`` dir — where this code runs from."""
    return Path(__file__).resolve().parent


async def perform_install(hass: HomeAssistant, checker: UpdateChecker) -> dict:
    """Run the full self-update. Returns a status dict; raises InstallError.

    Refuses cleanly (InstallError) if there's nothing newer to install or
    the downloaded payload doesn't match the tag. On success the live
    integration dir has already been swapped and an HA restart is scheduled.
    """
    status = await checker.async_status()
    if not status.get("update_available"):
        raise InstallError("no update available")

    target_version = status.get("latest_version")
    download_url, signature_url = await checker.async_artifact_urls()
    if not download_url:
        raise InstallError("release has no casasmart.zip asset")
    if not signature_url:
        raise InstallError("release is not signed (no casasmart.zip.sig)")

    _LOGGER.info("Self-update: installing %s from %s", target_version, download_url)

    # Stage everything under one temp dir we always clean up. The new tree is
    # copied into the live (same-filesystem) config dir by swap_integration_dir,
    # so a cross-filesystem temp location is fine here.
    with tempfile.TemporaryDirectory(prefix="casasmart-update-") as staging:
        staging_path = Path(staging)
        archive = staging_path / "release.zip"
        await _download_archive(hass, download_url, archive)
        signature = staging_path / "release.zip.sig"
        await _download_archive(hass, signature_url, signature)
        await hass.async_add_executor_job(_verify_archive, archive, signature)

        extracted = staging_path / "extracted"
        _extract_zip(archive, extracted)

        new_dir = locate_integration_dir(extracted, DOMAIN)
        if new_dir is None:
            raise InstallError("downloaded release has no custom_components/casasmart")

        new_version = read_manifest_version(new_dir)
        if not versions_match(target_version, new_version):
            raise InstallError(
                f"version mismatch: release tag {target_version!r} but "
                f"downloaded manifest is {new_version!r}"
            )

        backup = swap_integration_dir(_integration_dir(), new_dir)

    _LOGGER.warning(
        "Self-update: integration swapped to %s (backup at %s); restarting HA",
        target_version,
        backup,
    )
    _schedule_restart(hass)
    return {"installing": True, "target_version": target_version}


async def _download_archive(hass: HomeAssistant, url: str, dest: Path) -> None:
    """Stream a release archive to ``dest`` in chunks (never load it whole)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url, headers=_DOWNLOAD_HEADERS, timeout=_DOWNLOAD_TIMEOUT
        ) as response:
            if response.status != HTTPStatus.OK:
                raise InstallError(
                    f"download failed: GitHub returned {response.status}"
                )
            with dest.open("wb") as handle:
                async for chunk in response.content.iter_chunked(65536):
                    handle.write(chunk)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise InstallError(f"download failed: {err}") from err


def _verify_archive(archive: Path, signature: Path) -> None:
    """Refuse an archive the release key didn't sign (runs in the executor)."""
    sig = signature.read_bytes()
    if not sig or len(sig) > _MAX_SIGNATURE_BYTES:
        raise InstallError("release signature is missing or malformed")
    verify_release_signature(archive.read_bytes(), sig, UPDATE_SIGNING_PUBLIC_KEY_B64)


def _extract_zip(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest``, rejecting any entry that escapes it."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                target = (dest / member).resolve()
                # Path containment, not a string prefix: "<dest>_x/..." shares
                # the prefix but is outside the staging dir.
                if not target.is_relative_to(root):
                    raise InstallError(f"unsafe path in archive: {member}")
            bundle.extractall(dest)
    except zipfile.BadZipFile as err:
        raise InstallError(f"downloaded file is not a valid zip: {err}") from err


def _schedule_restart(hass: HomeAssistant) -> None:
    """Restart HA after a short grace period so the HTTP reply flushes first."""

    async def _restart_later() -> None:
        await asyncio.sleep(_RESTART_GRACE_SECONDS)
        _LOGGER.warning("Self-update: restarting Home Assistant now")
        await hass.services.async_call("homeassistant", "restart", blocking=False)

    hass.async_create_task(_restart_later())
