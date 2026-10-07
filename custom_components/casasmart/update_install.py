"""The self-update install (POST /api/casasmart/update/install).

Re-checks for a newer release, downloads its casasmart.zip and signature,
verifies the signature against the pinned release key before extracting
anything, checks the manifest version against the tag, swaps the new tree in
(keeping the old one as the rollback) and restarts Home Assistant once the
reply has gone out. One install runs at a time, and none while a swap waits
for its restart. To roll back by hand, stop Home Assistant and put
<config>/casasmart/update/rollback in place of custom_components/casasmart.
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

from .const import DATA_DIR_NAME, DOMAIN, UPDATE_SIGNING_PUBLIC_KEY_B64
from .update import (
    STAGING_DIR_NAME,
    UPDATE_DIR_NAME,
    InstallError,
    clear_legacy_update_dirs,
    locate_integration_dir,
    read_manifest_version,
    swap_integration_dir,
    verify_release_signature,
    versions_match,
)
from .update_api import UpdateChecker

_LOGGER = logging.getLogger(__name__)

# Longer than the status check's timeout, but bounded so a stuck download ends.
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
# Asset downloads ignore Accept; it matches the hub's other GitHub requests.
_DOWNLOAD_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "CasaSmart-Hub",
}
# An Ed25519 signature is 64 bytes; anything much larger is not one.
_MAX_SIGNATURE_BYTES = 1024
# Seconds to let the HTTP response reach the app before HA restarts.
_RESTART_GRACE_SECONDS = 2.0
# hass.data[DOMAIN] keys shared by both listeners' views: the install lock, and
# the version a finished swap is waiting to restart into.
_INSTALL_LOCK_KEY = "update_install_lock"
_SWAPPED_VERSION_KEY = "update_swapped_version"
# The CasaSmart app shows "Update already in progress" for a 409 whose error
# contains this phrase, so it must not change.
_IN_PROGRESS = "update already in progress"


def _integration_dir() -> Path:
    """The live custom_components/casasmart dir, where this code runs from."""
    return Path(__file__).resolve().parent


def _update_dir(hass: HomeAssistant) -> Path:
    """<config>/casasmart/update: the self-update's working dirs."""
    return Path(hass.config.path(DATA_DIR_NAME, UPDATE_DIR_NAME))


async def async_clear_legacy_update_dirs(hass: HomeAssistant) -> None:
    """Move or remove self-update dirs earlier versions left in custom_components.

    Runs at setup, since Home Assistant may load such a copy instead of the
    integration. Every outcome is logged, and a failure never fails setup.
    """
    try:
        actions = await hass.async_add_executor_job(
            clear_legacy_update_dirs, _integration_dir(), _update_dir(hass)
        )
    except OSError as err:
        _LOGGER.warning(
            "Could not check custom_components for leftover self-update "
            "directories: %s",
            err,
        )
        return
    for action in actions:
        if action.error is not None:
            _LOGGER.warning(
                "Could not remove the leftover self-update directory %s (%s); "
                "remove it by hand, or Home Assistant may load it instead of "
                "the integration",
                action.path,
                action.error,
            )
        elif action.moved_to is not None:
            _LOGGER.info(
                "Moved the self-update rollback %s to %s",
                action.path,
                action.moved_to,
            )
        else:
            _LOGGER.info("Removed the leftover self-update directory %s", action.path)


async def perform_install(hass: HomeAssistant, checker: UpdateChecker) -> dict:
    """Run the self-update and return the reply for the app.

    Raises InstallError when nothing is newer, the download doesn't check
    out, or another install is running or waiting for its restart. Until
    that restart the old code still reports the old version, so the same
    release would look new and a second swap would overwrite the rollback.
    The restart may have been refused (a config check failure), so the
    message asks for one rather than claiming it is under way.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    swapped = domain_data.get(_SWAPPED_VERSION_KEY)
    if swapped is not None:
        raise InstallError(
            f"{_IN_PROGRESS}: {swapped} is installed; restart Home Assistant to "
            "finish the update"
        )
    lock = domain_data.get(_INSTALL_LOCK_KEY)
    if lock is None:
        lock = domain_data[_INSTALL_LOCK_KEY] = asyncio.Lock()
    if lock.locked():
        raise InstallError(_IN_PROGRESS)
    async with lock:
        return await _async_install(hass, checker, domain_data)


async def _async_install(
    hass: HomeAssistant, checker: UpdateChecker, domain_data: dict
) -> dict:
    """The install itself; perform_install holds the install lock."""
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

    # One temp dir in staging holds the download and extraction.
    update_dir = _update_dir(hass)
    staging_path = Path(
        await hass.async_add_executor_job(_make_download_dir, update_dir)
    )
    try:
        archive = staging_path / "release.zip"
        await _download_archive(hass, download_url, archive)
        signature = staging_path / "release.zip.sig"
        await _download_archive(hass, signature_url, signature)
        await hass.async_add_executor_job(_verify_archive, archive, signature)
        backup = await hass.async_add_executor_job(
            _stage_and_swap,
            archive,
            staging_path / "extracted",
            target_version,
            update_dir,
        )
        # The new tree is live on disk now: no other install until the restart.
        domain_data[_SWAPPED_VERSION_KEY] = target_version
    finally:
        await hass.async_add_executor_job(shutil.rmtree, staging_path, True)

    _LOGGER.warning(
        "Self-update: integration swapped to %s (backup at %s); restarting HA",
        target_version,
        backup,
    )
    _schedule_restart(hass)
    return {"installing": True, "target_version": target_version}


async def _download_archive(hass: HomeAssistant, url: str, dest: Path) -> None:
    """Stream a release file to dest; the file writes run in the executor."""
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
            handle = await hass.async_add_executor_job(dest.open, "wb")
            try:
                async for chunk in response.content.iter_chunked(65536):
                    await hass.async_add_executor_job(handle.write, chunk)
            finally:
                await hass.async_add_executor_job(handle.close)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise InstallError(f"download failed: {err}") from err


def _verify_archive(archive: Path, signature: Path) -> None:
    """Refuse an archive the release key didn't sign (runs in the executor)."""
    sig = signature.read_bytes()
    if not sig or len(sig) > _MAX_SIGNATURE_BYTES:
        raise InstallError("release signature is missing or malformed")
    verify_release_signature(archive.read_bytes(), sig, UPDATE_SIGNING_PUBLIC_KEY_B64)


def _make_download_dir(update_dir: Path) -> str:
    """A fresh temp dir for one install's download and extraction (executor)."""
    staging = update_dir / STAGING_DIR_NAME
    try:
        staging.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="download-", dir=staging)
    except OSError as err:
        raise InstallError(f"cannot create the update dir {staging}: {err}") from err


def _stage_and_swap(
    archive: Path, extracted: Path, target_version: str, update_dir: Path
) -> Path:
    """Extract the verified zip, check it and swap it in (executor).

    Returns the rollback dir. Raises InstallError, with nothing swapped, if
    the archive has no casasmart integration or its version doesn't match.
    """
    _extract_zip(archive, extracted)
    new_dir = locate_integration_dir(extracted, DOMAIN)
    if new_dir is None:
        raise InstallError(
            "downloaded release has no casasmart integration (no manifest.json "
            "at the zip root or under custom_components/casasmart)"
        )
    new_version = read_manifest_version(new_dir)
    if not versions_match(target_version, new_version):
        raise InstallError(
            f"version mismatch: release tag {target_version!r} but "
            f"downloaded manifest is {new_version!r}"
        )
    return swap_integration_dir(_integration_dir(), new_dir, update_dir)


def _extract_zip(archive: Path, dest: Path) -> None:
    """Extract archive into dest, refusing any entry that escapes it."""
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
    """Restart HA after a short delay, so the HTTP reply goes out first."""

    async def _restart_later() -> None:
        await asyncio.sleep(_RESTART_GRACE_SECONDS)
        _LOGGER.warning("Self-update: restarting Home Assistant now")
        await hass.services.async_call("homeassistant", "restart", blocking=False)

    hass.async_create_task(_restart_later())
