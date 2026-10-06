"""Factory reset coverage: every storage table the hub opens is classified as
wiped or deliberately kept, so a new table can't silently survive a reset."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"
sys.path.insert(0, str(_PKG))

from const import FACTORY_RESET_KEPT_TABLES, FACTORY_RESET_TABLES  # noqa: E402


def _opened_tables() -> set[str]:
    names: set[str] = set()
    for path in _PKG.rglob("*.py"):
        names.update(re.findall(r'storage\.table\("([a-z0-9_]+)"\)', path.read_text()))
    return names


class FactoryResetTablesTests(unittest.TestCase):
    def test_every_table_is_wiped_or_deliberately_kept(self) -> None:
        classified = set(FACTORY_RESET_TABLES) | set(FACTORY_RESET_KEPT_TABLES)
        self.assertEqual(_opened_tables() - classified, set())

    def test_no_table_is_both_wiped_and_kept(self) -> None:
        self.assertEqual(
            set(FACTORY_RESET_TABLES) & set(FACTORY_RESET_KEPT_TABLES), set()
        )
        self.assertEqual(len(FACTORY_RESET_TABLES), len(set(FACTORY_RESET_TABLES)))

    def test_room_tags_and_move_receipts_are_wiped(self) -> None:
        # v2.2.0: the whole registry organization layer goes, tags included.
        for table in ("registry_room_tags", "registry_room_moves", "registry_rooms"):
            self.assertIn(table, FACTORY_RESET_TABLES)

    def test_house_configuration_is_kept(self) -> None:
        for table in ("tank_devices", "alarm_zones", "alarm_settings"):
            self.assertIn(table, FACTORY_RESET_KEPT_TABLES)

    def test_reset_handler_wipes_the_listed_tables(self) -> None:
        init = (_PKG / "__init__.py").read_text()
        self.assertIn("for table in FACTORY_RESET_TABLES:", init)
