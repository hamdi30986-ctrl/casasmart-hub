"""Tests for the fleet provisioning registry (pure, stdlib unittest).

Run: python3 -m unittest discover -s tools/fleet-registry/tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from registry import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_DECOMMISSIONED,
    FleetRegistry,
    RegistryError,
    _Zone,
    _Zones,
    load_zones,
)


def _zones() -> _Zones:
    return _Zones(
        domain="example.com",
        zones={
            "zone-north": _Zone(label="North Zone", prefix="zn"),
            "zone-south": _Zone(label="South Zone", prefix="zs"),
        },
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "fleet.json"
        self.reg = FleetRegistry(self.store, _zones())


class TestAllocation(_Base):
    def test_auto_allocates_sequential_slots(self) -> None:
        a = self.reg.register("h1", "A", "zone-north")
        b = self.reg.register("h2", "B", "zone-north")
        self.assertEqual(a.subdomain, "zn01")
        self.assertEqual(b.subdomain, "zn02")

    def test_zones_have_independent_pools(self) -> None:
        a = self.reg.register("h1", "A", "zone-north")
        r = self.reg.register("h2", "R", "zone-south")
        self.assertEqual(a.subdomain, "zn01")
        self.assertEqual(r.subdomain, "zs01")

    def test_decommission_frees_slot_for_reuse(self) -> None:
        self.reg.register("h1", "A", "zone-north")  # zn01
        self.reg.register("h2", "B", "zone-north")  # zn02
        self.reg.decommission("h1")  # frees zn01
        c = self.reg.register("h3", "C", "zone-north")
        self.assertEqual(c.subdomain, "zn01")

    def test_fqdn_uses_domain(self) -> None:
        a = self.reg.register("h1", "A", "zone-north")
        self.assertEqual(a.fqdn(self.reg.domain), "zn01.example.com")


class TestExplicitSubdomain(_Base):
    def test_claims_explicit_label(self) -> None:
        a = self.reg.register("h1", "Alice", "zone-north", subdomain="alice")
        self.assertEqual(a.subdomain, "alice")

    def test_explicit_label_lowercased_and_trimmed(self) -> None:
        a = self.reg.register("h1", "Alice", "zone-north", subdomain="  AlIce  ")
        self.assertEqual(a.subdomain, "alice")

    def test_collision_with_active_hub_refused(self) -> None:
        self.reg.register("h1", "Alice", "zone-north", subdomain="alice")
        with self.assertRaises(RegistryError):
            self.reg.register("h2", "Other", "zone-north", subdomain="alice")

    def test_can_reclaim_subdomain_of_decommissioned_hub(self) -> None:
        self.reg.register("h1", "Alice", "zone-north", subdomain="alice")
        self.reg.decommission("h1")
        b = self.reg.register("h2", "Alice2", "zone-north", subdomain="alice")
        self.assertEqual(b.subdomain, "alice")

    def test_invalid_subdomain_rejected(self) -> None:
        for bad in ["-bad", "bad-", "has_underscore", "UP!", "a" * 64, ""]:
            with self.subTest(bad=bad):
                with self.assertRaises(RegistryError):
                    self.reg.register("hx", "X", "zone-north", subdomain=bad)


class TestValidation(_Base):
    def test_unknown_zone_rejected(self) -> None:
        with self.assertRaises(RegistryError):
            self.reg.register("h1", "A", "atlantis")

    def test_duplicate_hub_id_rejected(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        with self.assertRaises(RegistryError):
            self.reg.register("h1", "B", "zone-south")

    def test_blank_hub_id_rejected(self) -> None:
        with self.assertRaises(RegistryError):
            self.reg.register("   ", "A", "zone-north")

    def test_blank_client_rejected(self) -> None:
        with self.assertRaises(RegistryError):
            self.reg.register("h1", "  ", "zone-north")


class TestQueries(_Base):
    def test_list_filters_by_zone_and_status(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        self.reg.register("h2", "B", "zone-south")
        self.reg.register("h3", "C", "zone-north")
        self.reg.decommission("h3")

        zn = self.reg.list(zone="zone-north")
        # sorted by subdomain: h1=zn01, h3=zn02
        self.assertEqual([h.hub_id for h in zn], ["h1", "h3"])

        active = self.reg.list(status=STATUS_ACTIVE)
        self.assertEqual({h.hub_id for h in active}, {"h1", "h2"})

    def test_show_unknown_raises(self) -> None:
        with self.assertRaises(RegistryError):
            self.reg.show("nope")


class TestUpdate(_Base):
    def test_update_mutable_fields(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        rec = self.reg.update("h1", ha_version="2026.6.1", tailscale_hostname="alice-hub")
        self.assertEqual(rec.ha_version, "2026.6.1")
        self.assertEqual(rec.tailscale_hostname, "alice-hub")

    def test_update_rejects_identity_fields(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        # subdomain/zone are non-editable identity fields; hub_id can't even be
        # passed as a kwarg (it's the positional target), so it's not testable here.
        for field in ["subdomain", "zone"]:
            with self.subTest(field=field):
                with self.assertRaises(RegistryError):
                    self.reg.update("h1", **{field: "x"})

    def test_update_rejects_bad_status(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        with self.assertRaises(RegistryError):
            self.reg.update("h1", status="zombie")

    def test_update_unknown_hub_raises(self) -> None:
        with self.assertRaises(RegistryError):
            self.reg.update("ghost", ha_version="1.0")

    def test_decommission_sets_status(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        rec = self.reg.decommission("h1")
        self.assertEqual(rec.status, STATUS_DECOMMISSIONED)


class TestPersistence(_Base):
    def test_survives_reload_from_disk(self) -> None:
        self.reg.register("h1", "Alice", "zone-north", subdomain="alice")
        self.reg.update("h1", ha_version="2026.6.1")

        fresh = FleetRegistry(self.store, _zones())
        rec = fresh.show("h1")
        self.assertEqual(rec.subdomain, "alice")
        self.assertEqual(rec.ha_version, "2026.6.1")

    def test_write_is_atomic_no_temp_left_behind(self) -> None:
        self.reg.register("h1", "A", "zone-north")
        leftovers = list(self.store.parent.glob(".reg-*.tmp"))
        self.assertEqual(leftovers, [])

    def test_empty_store_lists_nothing(self) -> None:
        self.assertEqual(self.reg.list(), [])


class TestZoneConfigLoader(unittest.TestCase):
    def test_loads_shipped_config(self) -> None:
        cfg = Path(__file__).resolve().parent.parent / "config" / "zones.json"
        zones = load_zones(cfg)
        self.assertEqual(zones.domain, "example.com")
        self.assertIn("zone-north", zones.zones)
        self.assertEqual(zones.zones["zone-north"].prefix, "zn")

    def test_missing_domain_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "z.json"
            p.write_text('{"zones": {"a": {"prefix": "a"}}}', encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_zones(p)

    def test_no_zones_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "z.json"
            p.write_text('{"domain": "x.sa", "zones": {}}', encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_zones(p)


if __name__ == "__main__":
    unittest.main()
