"""Tests for discovery.py — the mDNS advertiser's pure builder (B6).

Covers descriptor/TXT/instance-name construction only; the zeroconf
lifecycle wrapper (:class:`MdnsAdvertiser`) is verified live — the app
side discovers the genuine record on the LAN, and the dev hub loads the
advertiser with zero errors. Pure builder = stdlib only, imported
directly (the package ``__init__`` pulls in ``homeassistant``).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from discovery import (  # noqa: E402
    DEFAULT_HUB_NAME,
    SERVICE_TYPE,
    TXT_SCHEMA_VERSION,
    build_instance_name,
    build_service_descriptor,
    build_txt_records,
)

FP = "ab6ebef62a046c79914ebed984efb55963d5e06ac4f1eca327b9acfe765a23fb"


class TxtRecordTests(unittest.TestCase):
    def test_carries_id_api_and_schema_version(self) -> None:
        txt = build_txt_records(hub_id=FP, hub_name="Villa", api_version=1)
        self.assertEqual(txt["id"], FP.encode())
        self.assertEqual(txt["api"], b"1")
        self.assertEqual(txt["v"], TXT_SCHEMA_VERSION.encode())
        self.assertEqual(txt["name"], b"Villa")

    def test_name_dropped_when_blank(self) -> None:
        for name in (None, "", "   "):
            txt = build_txt_records(hub_id=FP, hub_name=name, api_version=1)
            self.assertNotIn("name", txt, f"blank name {name!r} must not publish")

    def test_empty_id_rejected(self) -> None:
        # An advertiser with no stable identity is unusable for matching —
        # fail loud at build, never broadcast an id-less record.
        with self.assertRaises(ValueError):
            build_txt_records(hub_id="", hub_name="x", api_version=1)

    def test_api_version_coerced_to_str(self) -> None:
        txt = build_txt_records(hub_id=FP, hub_name=None, api_version=2)
        self.assertEqual(txt["api"], b"2")

    def test_overlong_name_truncated(self) -> None:
        txt = build_txt_records(hub_id=FP, hub_name="x" * 200, api_version=1)
        self.assertLessEqual(len(txt["name"]), 63)


class InstanceNameTests(unittest.TestCase):
    def test_appends_short_fingerprint_for_uniqueness(self) -> None:
        name = build_instance_name("Villa", FP)
        self.assertEqual(name, "Villa (ab6ebef6)")

    def test_default_name_when_unset(self) -> None:
        self.assertTrue(build_instance_name(None, FP).startswith(DEFAULT_HUB_NAME))
        self.assertTrue(build_instance_name("  ", FP).startswith(DEFAULT_HUB_NAME))

    def test_two_same_named_hubs_get_distinct_labels(self) -> None:
        a = build_instance_name("Home", FP)
        b = build_instance_name("Home", "00112233445566778899")
        self.assertNotEqual(a, b)

    def test_label_capped_at_63_bytes(self) -> None:
        self.assertLessEqual(len(build_instance_name("n" * 100, FP)), 63)


class ServiceDescriptorTests(unittest.TestCase):
    def test_full_descriptor(self) -> None:
        d = build_service_descriptor(
            hub_id=FP, hub_name="Villa", api_version=1, port=8443
        )
        self.assertEqual(d.service_type, SERVICE_TYPE)
        self.assertEqual(d.port, 8443)
        self.assertEqual(d.instance_name, "Villa (ab6ebef6)")
        self.assertEqual(d.full_name, f"Villa (ab6ebef6).{SERVICE_TYPE}")
        self.assertEqual(d.properties["id"], FP.encode())

    def test_invalid_port_rejected(self) -> None:
        for port in (0, -1, 70000):
            with self.assertRaises(ValueError):
                build_service_descriptor(
                    hub_id=FP, hub_name="v", api_version=1, port=port
                )


if __name__ == "__main__":
    unittest.main()
