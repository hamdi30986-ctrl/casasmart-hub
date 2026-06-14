"""Self-update execution (Track B — B5, Piece 3) — the install action.

``POST /api/casasmart/update/install`` (owner/admin-only, ``update.install``)
turns the "update available" state Piece 1 reports into an actual upgrade:

    1. Re-check the latest release; refuse (409) if nothing is newer.
    2. Download the release artifact (a ``.zip`` asset, else the source
       zipball) to a temp file.
    3. Extract it, locate ``custom_components/casasmart`` inside, and verify
       its ``manifest.json`` version matches the tag we fetched.
    4. Atomically swap the live integration dir for the new tree, keeping a
       ``.bak`` rollback.
    5. Schedule an HA restart *after* the HTTP response flushes, so the app
       gets a clean "installing" reply before the connection drops; the
       container's restart policy brings HA back on the new code.

The filesystem mechanics (locate / version-match / atomic swap) are pure and
live in ``update.py`` so they unit-test with temp dirs. This module owns the
network download, the zip extraction, and the HA restart — the parts that
need a running hub and are proven live rather than in unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import zipfile
from http import HTTPStatus
from pathlib import Path

import aiohttp

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .update import (
    InstallError,
    locate_integration_dir,
    read_manifest_version,
    swap_integration_dir,
    versions_match,
)
from .update_api import UpdateChecker

_LOGGER = logging.getLogger(__name__)

# Downloading a release tarball is heavier than the status poll — give it
# room, but still bounded so a wedged transfer can't hang forever.
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
_DOWNLOAD_HEADERS = {
    "Accept": "application/octet-stream",
    "User-Agent": "CasaSmart-Hub",
}
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
    download_url = await checker.async_download_url()
    if not download_url:
        raise InstallError("release has no downloadable artifact")

    _LOGGER.info("Self-update: installing %s from %s", target_version, download_url)

    # Stage everything under one temp dir we always clean up. The new tree is
    # copied into the live (same-filesystem) config dir by swap_integration_dir,
    # so a cross-filesystem temp location is fine here.
    with tempfile.TemporaryDirectory(prefix="casasmart-update-") as staging:
        staging_path = Path(staging)
        archive = staging_path / "release.zip"
        await _download_archive(hass, download_url, archive)

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
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise InstallError(f"download failed: {err}") from err


def _extract_zip(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest``, rejecting path-traversal entries."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
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
