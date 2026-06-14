"""Unit tests for the pure self-update logic (B5).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from update import ReleaseInfo, is_newer, parse_release  # noqa: E402


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
            "draft": False,
            "prerelease": False,
        }
        base.update(overrides)
        return base

    def test_full_release(self):
        info = parse_release(self._payload())
        self.assertEqual(
            info,
            ReleaseInfo(
                version="v0.2.0",
                changelog="## What's new\n- Faster pairing",
                published_at="2026-06-14T12:00:00Z",
                release_url="https://github.com/casasmart/casasmart-hub/releases/tag/v0.2.0",
            ),
        )

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


if __name__ == "__main__":
    unittest.main()
