"""admin/registry lists HA devices without the device-registry access that
Home Assistant 2026.9 deprecates, and still works on older releases."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hastubs import install_casasmart_package, install_homeassistant_stubs

install_homeassistant_stubs()
install_casasmart_package()

try:
    from casasmart import admin_api

    _ERR = None
except Exception as err:  # the admin view needs more of HA than the stubs carry
    _ERR = err


class _Entry:
    def __init__(self, device_id: str) -> None:
        self.id = device_id


class _Since2026_9:
    """HA 2026.9+: iterating yields entries; mapping access is deprecated."""

    def __init__(self, entries) -> None:
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __getitem__(self, key):
        raise AssertionError("deprecated mapping access")

    def __getattr__(self, name):
        raise AssertionError(f"deprecated mapping access: {name}")


@unittest.skipIf(_ERR, f"admin_api unimportable: {_ERR}")
class DeviceEntriesTests(unittest.TestCase):
    def test_new_registry_is_iterated_without_mapping_access(self) -> None:
        entries = [_Entry("a"), _Entry("b")]
        registry = types.SimpleNamespace(devices=_Since2026_9(entries))
        self.assertEqual(admin_api._device_entries(registry), entries)

    def test_older_registry_iterates_ids_and_looks_them_up(self) -> None:
        a, b = _Entry("a"), _Entry("b")
        registry = types.SimpleNamespace(devices={"a": a, "b": b})
        self.assertEqual(admin_api._device_entries(registry), [a, b])


if __name__ == "__main__":
    unittest.main()
