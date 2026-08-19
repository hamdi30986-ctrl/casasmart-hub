"""CasaSmart runtime component."""

from __future__ import annotations

import asyncio
import logging
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



_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)




_DOWNLOAD_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "CasaSmart-Hub",
}

_RESTART_GRACE_SECONDS = 2.0


def _integration_dir() -> Path:
    """CasaSmart runtime component."""
    return Path(__file__).resolve().parent


async def perform_install(hass: HomeAssistant, checker: UpdateChecker) -> dict:
    """CasaSmart runtime component."""
    status = await checker.async_status()
    if not status.get("update_available"):
        raise InstallError("no update available")

    target_version = status.get("latest_version")
    download_url = await checker.async_download_url()
    if not download_url:
        raise InstallError("release has no downloadable artifact")

    _LOGGER.info("Self-update: installing %s from %s", target_version, download_url)




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
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""
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
    """CasaSmart runtime component."""

    async def _restart_later() -> None:
        await asyncio.sleep(_RESTART_GRACE_SECONDS)
        _LOGGER.warning("Self-update: restarting Home Assistant now")
        await hass.services.async_call("homeassistant", "restart", blocking=False)

    hass.async_create_task(_restart_later())
