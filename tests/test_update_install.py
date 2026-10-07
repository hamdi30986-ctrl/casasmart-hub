"""Since 2.3.0: the self-update installs only a signed casasmart.zip, and extraction
can't write outside its staging dir. One install runs at a time, and none once
a swap is waiting for its restart."""

from __future__ import annotations

import asyncio
import base64
import builtins
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

import view_harness as H  # noqa: E402
from casasmart import update, update_install  # noqa: E402
from casasmart.update import InstallError  # noqa: E402
from casasmart.update_api import CasaSmartUpdateInstallView  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
)

ZIP_URL = "https://github.com/x/casasmart-hub/releases/download/v9.9.9/casasmart.zip"
SIG_URL = ZIP_URL + ".sig"


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return buf.getvalue()


class _Checker:
    def __init__(self, sig_url: str | None = SIG_URL, latest: str = "v9.9.9") -> None:
        self._sig_url = sig_url
        self._latest = latest

    async def async_status(self) -> dict:
        return {"update_available": True, "latest_version": self._latest}

    async def async_artifact_urls(self):
        return ZIP_URL, self._sig_url


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._data), size):
            await asyncio.sleep(0)
            yield self._data[start : start + size]


class _Download:
    def __init__(self, data: bytes) -> None:
        self.status = 200
        self.content = _Body(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _ServingSession:
    """A GitHub stand-in for the real ``_download_archive``."""

    def __init__(self, served: dict[str, bytes]) -> None:
        self._served = served
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _Download(self._served[url])


class _Config:
    """``hass.config``: just the path helper the installer uses."""

    def __init__(self, config_dir: str) -> None:
        self.config_dir = config_dir

    def path(self, *parts: str) -> str:
        return os.path.join(self.config_dir, *parts)


# The config dir of the tests that don't look at the data dir themselves.
_SHARED_CONFIG = tempfile.TemporaryDirectory()


class _Hass:
    def __init__(self, config_dir: str | None = None) -> None:
        self.data: dict = {}
        self.config = _Config(config_dir or _SHARED_CONFIG.name)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _RecordingHass(_Hass):
    """Knows whether the code it is running is inside an executor job."""

    def __init__(self, config_dir: str | None = None) -> None:
        super().__init__(config_dir)
        self.in_executor = False

    async def async_add_executor_job(self, func, *args):
        self.in_executor = True
        try:
            return func(*args)
        finally:
            self.in_executor = False


class PerformInstallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._real_download = update_install._download_archive
        self.key = Ed25519PrivateKey.generate()
        pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._patch("UPDATE_SIGNING_PUBLIC_KEY_B64", base64.b64encode(pub).decode())
        # The HACS layout: integration files at the zip root.
        self.archive = _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"domain": "casasmart", "version": "9.9.9"}
                ),
                "__init__.py": "",
            }
        )
        self.served = {ZIP_URL: self.archive, SIG_URL: self.key.sign(self.archive)}

        async def fake_download(hass, url, dest: Path) -> None:
            dest.write_bytes(self.served[url])

        self.swaps: list[str | None] = []
        self.update_dirs: list[Path] = []

        def fake_swap(current, new_dir, update_dir):
            self.swaps.append(update_install.read_manifest_version(new_dir))
            self.update_dirs.append(Path(update_dir))
            return Path(update_dir) / "rollback"

        self.restarts: list[bool] = []
        self._patch("_download_archive", fake_download)
        self._patch("swap_integration_dir", fake_swap)
        self._patch("_schedule_restart", lambda hass: self.restarts.append(True))

    def _patch(self, name: str, value) -> None:
        patcher = mock.patch.object(update_install, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_signed_release_is_installed(self) -> None:
        hass = _Hass()
        result = await update_install.perform_install(hass, _Checker())
        self.assertEqual(result, {"installing": True, "target_version": "v9.9.9"})
        self.assertEqual(self.swaps, ["9.9.9"])
        # The swap works in the hub's data dir, not in custom_components.
        self.assertEqual(
            self.update_dirs, [Path(hass.config.path("casasmart", "update"))]
        )
        self.assertEqual(self.restarts, [True])

    async def test_nothing_but_the_live_tree_is_left_in_custom_components(
        self,
    ) -> None:
        # The real swap: Home Assistant scans every directory in
        # custom_components and takes the domain from its manifest, so a
        # rollback beside the live tree could be loaded in its place.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Path(tmp.name)
        live = config / "custom_components" / "casasmart"
        live.mkdir(parents=True)
        (live / "manifest.json").write_text(
            json.dumps({"domain": "casasmart", "version": "2.3.0"})
        )
        self._patch("_integration_dir", lambda: live)
        self._patch("swap_integration_dir", update.swap_integration_dir)

        await update_install.perform_install(_Hass(tmp.name), _Checker())

        self.assertEqual(sorted(p.name for p in live.parent.iterdir()), ["casasmart"])
        self.assertEqual(update.read_manifest_version(live), "9.9.9")
        rollback = config / "casasmart" / "update" / "rollback"
        self.assertEqual(update.read_manifest_version(rollback), "2.3.0")
        # The download and the staged copy are gone too.
        self.assertEqual(list((rollback.parent / "staging").iterdir()), [])
        self.assertEqual(self.restarts, [True])

    async def test_unsigned_release_is_refused(self) -> None:
        with self.assertRaises(InstallError) as ctx:
            await update_install.perform_install(_Hass(), _Checker(sig_url=None))
        self.assertIn("not signed", str(ctx.exception))
        self.assertEqual(self.swaps, [])

    async def test_bad_signature_is_refused_before_extracting(self) -> None:
        self.served[SIG_URL] = Ed25519PrivateKey.generate().sign(self.archive)
        extracted: list = []
        self._patch("_extract_zip", lambda *args: extracted.append(args))
        with self.assertRaises(InstallError):
            await update_install.perform_install(_Hass(), _Checker())
        self.assertEqual(extracted, [])
        self.assertEqual(self.swaps, [])

    async def test_file_work_runs_in_the_executor_not_the_event_loop(self) -> None:
        hass = _RecordingHass()
        seen: list[tuple[str, bool]] = []
        real_extract = update_install._extract_zip
        fake_swap = update_install.swap_integration_dir

        def extract(archive, dest):
            seen.append(("extract", hass.in_executor))
            real_extract(archive, dest)

        def swap(current, new_dir, update_dir):
            seen.append(("swap", hass.in_executor))
            return fake_swap(current, new_dir, update_dir)

        self._patch("_extract_zip", extract)
        self._patch("swap_integration_dir", swap)
        await update_install.perform_install(hass, _Checker())
        self.assertEqual(seen, [("extract", True), ("swap", True)])

    async def test_no_blocking_file_io_on_the_event_loop(self) -> None:
        # Home Assistant logs "Detected blocking call to open/scandir ...
        # inside the event loop" for these calls; the real download writes the
        # archive and the staging dir is removed afterwards, so both count.
        hass = _RecordingHass()
        on_loop: list[str] = []

        def watch(owner, name):
            original = getattr(owner, name)

            def wrapper(*args, **kwargs):
                if not hass.in_executor:
                    on_loop.append(name)
                return original(*args, **kwargs)

            patcher = mock.patch.object(owner, name, wrapper)
            patcher.start()
            self.addCleanup(patcher.stop)

        session = _ServingSession(self.served)
        patcher = mock.patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            lambda hass: session,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._patch("_download_archive", self._real_download)
        for owner, name in (
            (builtins, "open"),
            (Path, "open"),
            (Path, "read_bytes"),
            (Path, "read_text"),
            (Path, "write_bytes"),
            (Path, "write_text"),
            (os, "scandir"),
            (os, "listdir"),
            (os, "walk"),
        ):
            watch(owner, name)

        result = await update_install.perform_install(hass, _Checker())
        self.assertEqual(result, {"installing": True, "target_version": "v9.9.9"})
        self.assertEqual(session.urls, [ZIP_URL, SIG_URL])
        self.assertEqual(self.swaps, ["9.9.9"])
        self.assertEqual(on_loop, [])

    async def test_manifest_version_must_match_the_tag(self) -> None:
        with self.assertRaises(InstallError) as ctx:
            await update_install.perform_install(_Hass(), _Checker(latest="v9.9.8"))
        self.assertIn("version mismatch", str(ctx.exception))
        self.assertEqual(self.swaps, [])

    # -- one install at a time ---------------------------------------------------

    def _hold_first_download(self) -> asyncio.Event:
        """Park the first install in its download until the event is set."""
        gate = asyncio.Event()
        self.downloads: list[str] = []

        async def download(hass, url, dest: Path) -> None:
            self.downloads.append(url)
            if len(self.downloads) == 1:
                await gate.wait()
            dest.write_bytes(self.served[url])

        self._patch("_download_archive", download)
        return gate

    async def _until_downloading(self) -> None:
        while not self.downloads:
            await asyncio.sleep(0)

    async def test_a_second_install_while_one_runs_is_refused(self) -> None:
        hass = _Hass()
        gate = self._hold_first_download()
        first = asyncio.create_task(update_install.perform_install(hass, _Checker()))
        await self._until_downloading()

        with self.assertRaises(InstallError) as ctx:
            await update_install.perform_install(hass, _Checker())
        self.assertIn("already in progress", str(ctx.exception))

        gate.set()
        self.assertEqual(await first, {"installing": True, "target_version": "v9.9.9"})
        self.assertEqual(self.swaps, ["9.9.9"])
        self.assertEqual(self.restarts, [True])

    async def test_no_second_install_once_swapped_until_the_restart(self) -> None:
        # The running code still reports the old version, so the release looks
        # new again; a second swap would replace the only rollback.
        hass = _Hass()
        await update_install.perform_install(hass, _Checker())
        with self.assertRaises(InstallError) as ctx:
            await update_install.perform_install(hass, _Checker())
        self.assertIn("already in progress", str(ctx.exception))
        # The restart may have been refused (a config check failure), so the
        # message asks for one rather than claiming it is under way.
        self.assertIn("restart Home Assistant to finish", str(ctx.exception))
        self.assertEqual(self.swaps, ["9.9.9"])
        self.assertEqual(self.restarts, [True])

    async def test_a_failed_install_does_not_block_the_next(self) -> None:
        hass = _Hass()
        with self.assertRaises(InstallError):
            await update_install.perform_install(hass, _Checker(latest="v9.9.8"))

        def failing_swap(current, new_dir, update_dir):
            raise InstallError("failed to swap in new integration tree: boom")

        with (
            mock.patch.object(update_install, "swap_integration_dir", failing_swap),
            self.assertRaises(InstallError),
        ):
            await update_install.perform_install(hass, _Checker())
        self.assertEqual(self.restarts, [])

        await update_install.perform_install(hass, _Checker())
        self.assertEqual(self.swaps, ["9.9.9"])
        self.assertEqual(self.restarts, [True])

    async def test_the_app_gets_409_already_in_progress(self) -> None:
        # The phone app reads a 409's {"error": ...} and shows "Update already
        # in progress" when it contains "already in progress". Two views, as
        # the plain and TLS listeners each build their own.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hass, runtime = H.make_hub(tmp.name)
        self.addCleanup(runtime.storage.close)
        hass.config = _Config(tmp.name)
        _, headers = H.session(runtime.auth, role="admin")
        checker = _Checker()
        plain = CasaSmartUpdateInstallView(hass, checker)
        tls = CasaSmartUpdateInstallView(hass, checker)
        gate = self._hold_first_download()

        first = asyncio.create_task(plain.post(H.FakeRequest(headers=headers)))
        await self._until_downloading()
        status, body = H.read_response(await tls.post(H.FakeRequest(headers=headers)))
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "update already in progress"})

        gate.set()
        status, body = H.read_response(await first)
        self.assertEqual(status, 202)

        status, body = H.read_response(await tls.post(H.FakeRequest(headers=headers)))
        self.assertEqual(status, 409)
        self.assertIn("already in progress", body["error"].lower())
        self.assertEqual(self.swaps, ["9.9.9"])


class LegacyDirCleanupTests(unittest.IsolatedAsyncioTestCase):
    """At setup, self-update dirs that earlier versions left beside the live
    integration are moved out of custom_components or removed, and logged."""

    async def test_leftovers_are_moved_or_removed_and_logged(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Path(tmp.name)
        cc = config / "custom_components"
        live = cc / "casasmart"
        for name, version in (
            ("casasmart", "2.4.0"),
            ("casasmart.bak", "2.3.0"),
            ("casasmart.new-deadbeef", "2.4.0"),
        ):
            (cc / name).mkdir(parents=True)
            (cc / name / "manifest.json").write_text(
                json.dumps({"domain": "casasmart", "version": version})
            )
        (cc / "hacs").mkdir()

        hass = _RecordingHass(tmp.name)
        with (
            mock.patch.object(update_install, "_integration_dir", lambda: live),
            self.assertLogs(update_install._LOGGER, "INFO") as logs,
        ):
            await update_install.async_clear_legacy_update_dirs(hass)

        self.assertEqual(sorted(p.name for p in cc.iterdir()), ["casasmart", "hacs"])
        rollback = config / "casasmart" / "update" / "rollback"
        self.assertEqual(update.read_manifest_version(rollback), "2.3.0")
        self.assertEqual(update.read_manifest_version(live), "2.4.0")
        text = "\n".join(logs.output)
        self.assertIn("casasmart.bak", text)
        self.assertIn(str(rollback), text)
        self.assertIn("casasmart.new-deadbeef", text)

    async def test_a_dir_it_cannot_remove_is_a_warning(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cc = Path(tmp.name) / "custom_components"
        live = cc / "casasmart"
        live.mkdir(parents=True)
        (cc / "casasmart.new-deadbeef").mkdir()

        def rmtree(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        with (
            mock.patch.object(update_install, "_integration_dir", lambda: live),
            mock.patch.object(update.shutil, "rmtree", rmtree),
            self.assertLogs(update_install._LOGGER, "WARNING") as logs,
        ):
            await update_install.async_clear_legacy_update_dirs(_Hass(tmp.name))
        self.assertIn("casasmart.new-deadbeef", logs.output[0])
        self.assertIn("Permission denied", logs.output[0])


class ExtractZipTests(unittest.TestCase):
    def _extract(self, files: dict[str, str]) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        archive = tmp / "release.zip"
        archive.write_bytes(_zip_bytes(files))
        dest = tmp / "extracted"
        update_install._extract_zip(archive, dest)
        return dest

    def test_normal_archive_extracts(self) -> None:
        dest = self._extract({"manifest.json": "{}", "sub/a.py": ""})
        self.assertTrue((dest / "sub" / "a.py").is_file())

    def test_sibling_prefix_escape_is_refused(self) -> None:
        # "<staging>/extracted_evil" shares the string prefix "<staging>/extracted".
        with self.assertRaises(InstallError):
            self._extract({"../extracted_evil/x.py": ""})

    def test_parent_escape_is_refused(self) -> None:
        with self.assertRaises(InstallError):
            self._extract({"../../escape.py": ""})
