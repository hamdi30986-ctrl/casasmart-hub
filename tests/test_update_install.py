"""Since 2.3.0: the self-update installs only a signed casasmart.zip, and extraction
can't write outside its staging dir."""

from __future__ import annotations

import base64
import io
import json
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

from casasmart import update_install  # noqa: E402
from casasmart.update import InstallError  # noqa: E402
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


class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _RecordingHass(_Hass):
    """Knows whether the code it is running is inside an executor job."""

    def __init__(self) -> None:
        self.in_executor = False

    async def async_add_executor_job(self, func, *args):
        self.in_executor = True
        try:
            return func(*args)
        finally:
            self.in_executor = False


class PerformInstallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
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

        def fake_swap(current, new_dir):
            self.swaps.append(update_install.read_manifest_version(new_dir))
            return Path("/backup")

        self.restarts: list[bool] = []
        self._patch("_download_archive", fake_download)
        self._patch("swap_integration_dir", fake_swap)
        self._patch("_schedule_restart", lambda hass: self.restarts.append(True))

    def _patch(self, name: str, value) -> None:
        patcher = mock.patch.object(update_install, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_signed_release_is_installed(self) -> None:
        result = await update_install.perform_install(_Hass(), _Checker())
        self.assertEqual(result, {"installing": True, "target_version": "v9.9.9"})
        self.assertEqual(self.swaps, ["9.9.9"])
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

        def swap(current, new_dir):
            seen.append(("swap", hass.in_executor))
            return fake_swap(current, new_dir)

        self._patch("_extract_zip", extract)
        self._patch("swap_integration_dir", swap)
        await update_install.perform_install(hass, _Checker())
        self.assertEqual(seen, [("extract", True), ("swap", True)])

    async def test_manifest_version_must_match_the_tag(self) -> None:
        with self.assertRaises(InstallError) as ctx:
            await update_install.perform_install(_Hass(), _Checker(latest="v9.9.8"))
        self.assertIn("version mismatch", str(ctx.exception))
        self.assertEqual(self.swaps, [])


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
