"""Unit tests for the pure self-update logic.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

import update
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from update import (
    InstallError,
    ReleaseInfo,
    is_newer,
    locate_integration_dir,
    parse_release,
    read_manifest_version,
    swap_integration_dir,
    verify_release_signature,
    versions_match,
)

ZIP_URL = (
    "https://github.com/casasmart/casasmart-hub/releases/download/v0.2.0/casasmart.zip"
)
SIG_URL = ZIP_URL + ".sig"


class TestIsNewer(unittest.TestCase):
    def test_higher_patch_minor_major_is_newer(self):
        self.assertTrue(is_newer("0.1.0", "0.1.1"))
        self.assertTrue(is_newer("0.1.0", "0.2.0"))
        self.assertTrue(is_newer("0.9.0", "1.0.0"))

    def test_same_or_older_is_not_newer(self):
        self.assertFalse(is_newer("1.0.0", "1.0.0"))
        self.assertFalse(is_newer("1.2.0", "1.1.9"))
        self.assertFalse(is_newer("2.0.0", "1.9.9"))

    def test_numeric_not_lexical(self):
        # 1.10.0 > 1.9.0 even though "10" < "9" as strings.
        self.assertTrue(is_newer("1.9.0", "1.10.0"))
        self.assertFalse(is_newer("1.10.0", "1.9.0"))

    def test_leading_v_and_length_mismatch(self):
        self.assertTrue(is_newer("v0.1", "v0.2.0"))
        self.assertFalse(is_newer("1.2", "1.2.0"))  # 1.2 == 1.2.0
        self.assertTrue(is_newer("1.2", "1.2.1"))

    def test_prerelease_tiebreak(self):
        # A final release beats a pre-release of the same base.
        self.assertTrue(is_newer("1.0.0-rc1", "1.0.0"))
        # ...but a pre-release of a base we already run is not newer.
        self.assertFalse(is_newer("1.0.0", "1.0.0-rc1"))
        # Same base, two pre-releases compare by suffix.
        self.assertTrue(is_newer("1.0.0-rc1", "1.0.0-rc2"))

    def test_unparsable_inputs(self):
        # Junk latest is never offered as an update.
        self.assertFalse(is_newer("1.0.0", "garbage"))
        self.assertFalse(is_newer("1.0.0", None))
        # Unparsable current => any real release shows as available.
        self.assertTrue(is_newer("dev", "1.0.0"))
        self.assertTrue(is_newer(None, "1.0.0"))


class TestParseRelease(unittest.TestCase):
    def _payload(self, **overrides):
        base = {
            "tag_name": "v0.2.0",
            "body": "## What's new\n- Faster pairing\n",
            "published_at": "2026-06-14T12:00:00Z",
            "html_url": "https://github.com/casasmart/casasmart-hub/releases/tag/v0.2.0",
            "zipball_url": "https://api.github.com/repos/casasmart/casasmart-hub/zipball/v0.2.0",
            "draft": False,
            "prerelease": False,
        }
        base.update(overrides)
        return base

    def _assets(self):
        return [
            {"name": "casasmart.zip", "browser_download_url": ZIP_URL},
            {"name": "casasmart.zip.sig", "browser_download_url": SIG_URL},
        ]

    def test_full_release(self):
        info = parse_release(self._payload(assets=self._assets()))
        self.assertEqual(
            info,
            ReleaseInfo(
                version="v0.2.0",
                changelog="## What's new\n- Faster pairing",
                published_at="2026-06-14T12:00:00Z",
                release_url="https://github.com/casasmart/casasmart-hub/releases/tag/v0.2.0",
                download_url=ZIP_URL,
                signature_url=SIG_URL,
            ),
        )

    def test_only_the_casasmart_zip_asset_is_the_artifact(self):
        # The HACS release asset by name; other zips (and checksums) are not it.
        info = parse_release(
            self._payload(
                assets=[
                    {"name": "checksums.txt", "browser_download_url": "x"},
                    {"name": "casasmart-0.2.0.zip", "browser_download_url": "y"},
                    *self._assets(),
                ]
            )
        )
        self.assertEqual(info.download_url, ZIP_URL)
        self.assertEqual(info.signature_url, SIG_URL)

    def test_zipball_is_never_used(self):
        # Since 2.3.0: the source zipball is not the release artifact and is never
        # signed, so a release without casasmart.zip has nothing to install.
        info = parse_release(self._payload(assets=[]))
        self.assertIsNone(info.download_url)
        self.assertIsNone(info.signature_url)

    def test_unsigned_release_has_no_signature_url(self):
        info = parse_release(self._payload(assets=self._assets()[:1]))
        self.assertEqual(info.download_url, ZIP_URL)
        self.assertIsNone(info.signature_url)

    def test_download_url_none_when_no_artifact(self):
        payload = self._payload()
        del payload["zipball_url"]
        info = parse_release(payload)
        self.assertIsNone(info.download_url)

    def test_draft_is_dropped(self):
        self.assertIsNone(parse_release(self._payload(draft=True)))

    def test_prerelease_is_kept(self):
        # parse keeps it; is_newer decides whether it counts.
        info = parse_release(self._payload(prerelease=True))
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "v0.2.0")

    def test_missing_or_blank_tag(self):
        self.assertIsNone(parse_release(self._payload(tag_name="")))
        self.assertIsNone(parse_release(self._payload(tag_name=None)))
        no_tag = self._payload()
        del no_tag["tag_name"]
        self.assertIsNone(parse_release(no_tag))

    def test_blank_changelog_becomes_none(self):
        info = parse_release(self._payload(body="   "))
        self.assertIsNone(info.changelog)
        info2 = parse_release(self._payload(body=None))
        self.assertIsNone(info2.changelog)

    def test_non_dict_payload(self):
        self.assertIsNone(parse_release(None))
        self.assertIsNone(parse_release([]))
        self.assertIsNone(parse_release("nope"))


class TestVersionsMatch(unittest.TestCase):
    def test_exact_and_v_prefixed_and_zero_padded(self):
        self.assertTrue(versions_match("v0.2.0", "0.2.0"))
        self.assertTrue(versions_match("0.2", "0.2.0"))
        self.assertTrue(versions_match("v1.0.0-rc1", "1.0.0-rc1"))

    def test_mismatch_is_rejected(self):
        self.assertFalse(versions_match("v0.2.0", "0.2.1"))
        self.assertFalse(versions_match("1.0.0", "1.0.0-rc1"))
        self.assertFalse(versions_match("v0.2.0", None))
        self.assertFalse(versions_match("garbage", "0.2.0"))


class TestLocateAndReadManifest(unittest.TestCase):
    def _make_release_tree(self, root: Path, version: str, depth_prefix: str) -> Path:
        """Build owner-repo-sha/custom_components/casasmart/manifest.json."""
        integ = root / depth_prefix / "custom_components" / "casasmart"
        integ.mkdir(parents=True)
        (integ / "manifest.json").write_text(
            json.dumps({"domain": "casasmart", "version": version})
        )
        return integ

    def test_locates_integration_in_zipball_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._make_release_tree(
                root, "0.2.0", "casasmart-casasmart-hub-abc123"
            )
            found = locate_integration_dir(root, "casasmart")
            self.assertEqual(found, expected)
            self.assertEqual(read_manifest_version(found), "0.2.0")

    def test_locates_integration_at_the_zip_root(self):
        # The packaged casasmart.zip (HACS layout) holds the files at the root.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(
                json.dumps({"domain": "casasmart", "version": "2.2.0"})
            )
            self.assertEqual(locate_integration_dir(root, "casasmart"), root)
            self.assertEqual(read_manifest_version(root), "2.2.0")

    def test_root_manifest_of_another_domain_is_not_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"domain": "other"}))
            self.assertIsNone(locate_integration_dir(root, "casasmart"))

    def test_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(locate_integration_dir(Path(tmp), "casasmart"))

    def test_prefers_shallowest_match(self):
        # A nested test fixture must not shadow the real integration dir.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shallow = self._make_release_tree(root, "0.2.0", "repo-sha")
            self._make_release_tree(root, "9.9.9", "repo-sha/tests/fixtures/bundle")
            self.assertEqual(locate_integration_dir(root, "casasmart"), shallow)

    def test_read_version_handles_bad_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(read_manifest_version(d))  # no manifest
            (d / "manifest.json").write_text("{not json")
            self.assertIsNone(read_manifest_version(d))


class TestSwapIntegrationDir(unittest.TestCase):
    def test_swap_replaces_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "casasmart"
            current.mkdir()
            (current / "old.py").write_text("# v1")

            new_source = root / "new_casasmart"
            new_source.mkdir()
            (new_source / "new.py").write_text("# v2")

            backup = swap_integration_dir(current, new_source)

            # Live dir now holds the new tree...
            self.assertTrue((current / "new.py").exists())
            self.assertFalse((current / "old.py").exists())
            # ...and the old tree is preserved in the backup for rollback.
            self.assertTrue((backup / "old.py").exists())
            self.assertEqual(backup.name, "casasmart.bak")

    def test_swap_overwrites_stale_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "casasmart"
            current.mkdir()
            (current / "cur.py").write_text("# cur")
            stale = root / "casasmart.bak"
            stale.mkdir()
            (stale / "stale.py").write_text("# stale")
            new_source = root / "new"
            new_source.mkdir()
            (new_source / "n.py").write_text("# n")

            backup = swap_integration_dir(current, new_source)
            self.assertTrue((backup / "cur.py").exists())
            self.assertFalse((backup / "stale.py").exists())

    def test_swap_keeps_the_previous_backup_until_it_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, new_source = _swap_fixture(Path(tmp), with_backup=True)
            backup = swap_integration_dir(current, new_source)
            self.assertEqual(_tree(current), _tree(new_source))
            self.assertEqual(_tree(backup), _version_tree("2.3.0"))
            self.assertEqual(_entries(Path(tmp)), ["casasmart", "casasmart.bak"])

    def test_swap_rejects_non_directory_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "casasmart"
            current.mkdir()
            with self.assertRaises(InstallError):
                swap_integration_dir(current, Path(tmp) / "does-not-exist")


def _version_tree(version: str) -> dict[str, str]:
    files = {f"mod{i}.py": f"# {version} {i}" for i in range(20)}
    files["manifest.json"] = json.dumps({"domain": "casasmart", "version": version})
    return files


def _write_tree(path: Path, version: str) -> Path:
    path.mkdir(parents=True)
    for name, content in _version_tree(version).items():
        (path / name).write_text(content)
    return path


def _tree(path: Path) -> dict[str, str]:
    return {f.name: f.read_text() for f in path.iterdir()}


def _entries(root: Path) -> list[str]:
    """What sits next to the live dir, minus the release staging dir."""
    return sorted(p.name for p in root.iterdir() if p.name != "release")


def _swap_fixture(root: Path, *, with_backup: bool) -> tuple[Path, Path]:
    """Live 2.3.0, optionally a 2.2.0 rollback beside it, and a 2.4.0 release."""
    current = _write_tree(root / "casasmart", "2.3.0")
    if with_backup:
        _write_tree(root / "casasmart.bak", "2.2.0")
    return current, _write_tree(root / "release", "2.4.0")


class TestSwapFailures(unittest.TestCase):
    """A swap that fails at any stage must leave the old live tree in place and
    must not lose the rollback it found. Every failure is an InstallError."""

    def _new_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _fail_rename(self, nth: int):
        """Make the nth os.rename of the swap fail (later ones, the undo, work)."""
        real_rename = update.os.rename
        calls = []

        def rename(src, dst):
            calls.append((src, dst))
            if len(calls) == nth:
                raise OSError(5, "I/O error")
            real_rename(src, dst)

        return mock.patch.object(update.os, "rename", side_effect=rename)

    def _fail_copy(self):
        real_copytree = shutil.copytree

        def copytree(src, dst, **kwargs):
            # Fail partway, after some files have landed.
            real_copytree(src, dst, ignore=lambda d, names: names[5:], **kwargs)
            raise shutil.Error([("x", "y", "No space left on device")])

        return mock.patch.object(update.shutil, "copytree", side_effect=copytree)

    def _assert_untouched(self, current: Path, *, with_backup: bool):
        self.assertEqual(_tree(current), _version_tree("2.3.0"))
        expected = ["casasmart"]
        if with_backup:
            backup = current.with_name("casasmart.bak")
            self.assertEqual(_tree(backup), _version_tree("2.2.0"))
            expected.append("casasmart.bak")
        # No staging copy or set-aside backup is left behind.
        self.assertEqual(_entries(current.parent), expected)

    def test_copy_failure(self):
        for with_backup in (True, False):
            with self.subTest(with_backup=with_backup):
                current, new_source = _swap_fixture(
                    self._new_root(), with_backup=with_backup
                )
                with self._fail_copy(), self.assertRaises(InstallError):
                    swap_integration_dir(current, new_source)
                self._assert_untouched(current, with_backup=with_backup)

    def test_rename_failure_at_each_stage(self):
        # With a backup: set the old backup aside, live -> .bak, copy -> live.
        # Without one the first of those is skipped.
        for with_backup, renames in ((True, 3), (False, 2)):
            for nth in range(1, renames + 1):
                with self.subTest(with_backup=with_backup, rename=nth):
                    current, new_source = _swap_fixture(
                        self._new_root(), with_backup=with_backup
                    )
                    with self._fail_rename(nth), self.assertRaises(InstallError):
                        swap_integration_dir(current, new_source)
                    self._assert_untouched(current, with_backup=with_backup)

    def test_overlapping_swap_is_refused_and_the_rollback_survives(self):
        # Swap A stops right after moving the live dir to .bak; swap B runs
        # meanwhile. B used to die on a bare FileNotFoundError, after already
        # deleting the .bak that A had just made.
        root = self._new_root()
        current, new_source = _swap_fixture(root, with_backup=True)
        real_rename = update.os.rename
        a_moved_live = threading.Event()
        b_done = threading.Event()

        def rename(src, dst):
            real_rename(src, dst)
            if (
                threading.current_thread().name == "A"
                and Path(src) == current
                and Path(dst).name == "casasmart.bak"
            ):
                a_moved_live.set()
                b_done.wait(5)

        results: dict[str, object] = {}

        def run(name):
            if name == "B":
                a_moved_live.wait(5)
            try:
                results[name] = swap_integration_dir(current, new_source)
            except Exception as err:
                results[name] = err
            if name == "B":
                b_done.set()

        with mock.patch.object(update.os, "rename", side_effect=rename):
            threads = [threading.Thread(target=run, args=(n,), name=n) for n in "AB"]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

        self.assertEqual(results["A"], root / "casasmart.bak")
        self.assertIsInstance(results["B"], InstallError)
        self.assertEqual(_tree(current), _version_tree("2.4.0"))
        self.assertEqual(_tree(root / "casasmart.bak"), _version_tree("2.3.0"))
        self.assertEqual(_entries(root), ["casasmart", "casasmart.bak"])


if __name__ == "__main__":
    unittest.main()


class TestVerifyReleaseSignature(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.pub_b64 = __import__("base64").b64encode(raw).decode()
        self.archive = b"PK\x03\x04 casasmart.zip bytes"

    def test_valid_signature_passes(self):
        verify_release_signature(
            self.archive, self.key.sign(self.archive), self.pub_b64
        )

    def test_tampered_archive_is_refused(self):
        sig = self.key.sign(self.archive)
        with self.assertRaises(InstallError):
            verify_release_signature(self.archive + b"!", sig, self.pub_b64)

    def test_other_key_is_refused(self):
        sig = Ed25519PrivateKey.generate().sign(self.archive)
        with self.assertRaises(InstallError):
            verify_release_signature(self.archive, sig, self.pub_b64)

    def test_unusable_key_is_refused(self):
        with self.assertRaises(InstallError):
            verify_release_signature(self.archive, b"x" * 64, "not base64!")

    def test_pinned_key_is_a_valid_ed25519_key(self):
        import re

        const = (
            Path(__file__).resolve().parent.parent
            / "custom_components/casasmart/const.py"
        ).read_text()
        pinned = re.search(r'UPDATE_SIGNING_PUBLIC_KEY_B64 = "([^"]+)"', const).group(1)
        with self.assertRaises(InstallError) as ctx:  # a random sig never verifies
            verify_release_signature(self.archive, b"\0" * 64, pinned)
        self.assertIn("does not verify", str(ctx.exception))
