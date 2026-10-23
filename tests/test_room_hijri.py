"""room_intel.hijri: when Ramadan is, by the Umm al-Qura calendar (no HA).

The table was generated from OpenJDK's HijrahChronology and Apple
Foundation's islamicUmmAlQura calendar (ICU), which agree on every year it
holds; these tests pin it to the dates Saudi Arabia announced and to the
published Umm al-Qura calendar, and check that the room activity log keeps
long enough to cover last year's Ramadan.
"""

import importlib
import sys
import unittest
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "casasmart"
if "habit_fixture" not in sys.modules:
    _package = ModuleType("habit_fixture")
    _package.__path__ = [str(_ROOT)]
    sys.modules["habit_fixture"] = _package
hijri = importlib.import_module("habit_fixture.room_intel.hijri")
activity = importlib.import_module("habit_fixture.room_intel.activity")

EPOCH = date(1970, 1, 1)


def day(iso):
    return (date.fromisoformat(iso) - EPOCH).days


def iso(number):
    return (EPOCH + timedelta(days=number)).isoformat()


class KnownDatesTests(unittest.TestCase):
    def test_ramadan_starts_and_eid(self):
        # (Hijri year, 1 Ramadan, 1 Shawwal = Eid al-Fitr)
        known = (
            # Announced by Saudi Arabia's Supreme Court.
            (1444, "2023-03-23", "2023-04-21"),
            (1445, "2024-03-11", "2024-04-10"),
            (1446, "2025-03-01", "2025-03-30"),
            (1447, "2026-02-18", "2026-03-20"),
            # The published Umm al-Qura calendar (a sighting may move a day).
            (1448, "2027-02-08", "2027-03-09"),
            (1449, "2028-01-28", "2028-02-26"),
        )
        for year, first, eid in known:
            with self.subTest(year=year):
                month = hijri.ramadan_of(day(first))
                self.assertEqual(month.year, year)
                self.assertEqual((iso(month.first), iso(month.last + 1)), (first, eid))
                self.assertIsNone(hijri.ramadan_of(day(first) - 1))
                self.assertIsNone(hijri.ramadan_of(day(eid)))
                self.assertEqual(hijri.ramadan_of(day(eid) - 1), month)

    def test_the_table_is_whole(self):
        months = hijri.MONTHS
        self.assertEqual((hijri.FIRST_YEAR, hijri.LAST_YEAR), (1440, 1500))
        self.assertEqual([m.year for m in months], list(range(1440, 1501)))
        for before, after in pairwise(months):
            with self.subTest(year=after.year):
                self.assertIn(after.days, (29, 30))
                # A lunar year is 354 or 355 days.
                self.assertIn(after.first - before.first, (354, 355))

    def test_outside_the_table_nothing_is_ramadan(self):
        self.assertIsNone(hijri.ramadan_of(day("2019-01-01")))
        self.assertIsNone(hijri.ramadan_of(day("2080-01-01")))
        self.assertIsNone(hijri.next_ramadan(day("2078-01-01")))
        self.assertEqual(list(hijri.ramadans_before(day("2019-05-06"))), [])


class LookupTests(unittest.TestCase):
    def test_ramadans_before_a_date_newest_first(self):
        during = day("2026-03-01")  # Ramadan 1447
        self.assertEqual(
            [m.year for m in hijri.ramadans_before(during)][:3], [1447, 1446, 1445]
        )
        # A Ramadan that starts on the date hasn't begun before it.
        first = day("2027-02-08")
        self.assertEqual(next(hijri.ramadans_before(first)).year, 1447)
        self.assertEqual(next(hijri.ramadans_before(first + 1)).year, 1448)

    def test_the_next_ramadan(self):
        self.assertEqual(hijri.next_ramadan(day("2026-10-23")).year, 1448)
        # During Ramadan, that one.
        self.assertEqual(hijri.next_ramadan(day("2027-03-08")).year, 1448)
        self.assertEqual(hijri.next_ramadan(day("2027-03-09")).year, 1449)


class RetentionTests(unittest.TestCase):
    def test_the_log_still_holds_last_years_ramadan_when_this_one_ends(self):
        # On the last day of every Ramadan, every day of the one before is
        # still inside the log's human retention (activity.HUMAN_RETENTION_DAYS),
        # with margin.
        months = hijri.MONTHS
        longest = max(after.last - before.first for before, after in pairwise(months))
        self.assertLessEqual(longest, 385)
        self.assertGreaterEqual(activity.HUMAN_RETENTION_DAYS - longest, 15)


if __name__ == "__main__":
    unittest.main()
