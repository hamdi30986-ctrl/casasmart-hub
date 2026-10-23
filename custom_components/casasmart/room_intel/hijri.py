"""When Ramadan is, by Saudi Arabia's Umm al-Qura calendar, offline.

The room habits learn Ramadan as a profile of its own (room_intel.habits).
The hub has no internet to ask. Umm al-Qura months come from an astronomical
rule at Makkah that King Abdulaziz City for Science and Technology applies
and publishes as a table; calendar software ships the table rather than
recompute the rule (ICU's islamic-umalqura calendar, used by Apple and
Android; Java's HijrahChronology, Hijrah-umalqura; .NET's
UmAlQuraCalendar), and so does the hub.

_RAMADAN holds, for each Hijri year, the Gregorian date of 1 Ramadan and the
month's length (29 or 30 days; 1 Shawwal is the day after). It was generated
from OpenJDK 26's HijrahChronology and Apple Foundation's islamicUmmAlQura
calendar (ICU), which agree on every year here; the Python package hijridate
2.6.0 agrees through 1452 AH (2030) and differs by a day in some later
years. Checked against Saudi Arabia's announced dates (Supreme Court):
1 Ramadan 1446 = 2025-03-01 (29 days, Eid 2025-03-30) and 1447 = 2026-02-18
(30 days, Eid 2026-03-20); the published Umm al-Qura calendar gives
1448 = 2027-02-08 (Eid 2027-03-09) and 1449 = 2028-01-28 (Eid 2028-02-26).
The official start follows a moon sighting and can differ from the
calendar by a day; the habits don't need the exact day.

Dates are local day numbers (1970-01-01 = 0), as in room_intel.habits.
Outside the table (before 1440 AH, after 1500 AH) no date is Ramadan.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from datetime import date
from typing import NamedTuple

# (Hijri year, 1 Ramadan, days in Ramadan)
_RAMADAN = (
    (1440, "2019-05-06", 29),
    (1441, "2020-04-24", 30),
    (1442, "2021-04-13", 30),
    (1443, "2022-04-02", 30),
    (1444, "2023-03-23", 29),
    (1445, "2024-03-11", 30),
    (1446, "2025-03-01", 29),
    (1447, "2026-02-18", 30),
    (1448, "2027-02-08", 29),
    (1449, "2028-01-28", 29),
    (1450, "2029-01-16", 29),
    (1451, "2030-01-05", 30),
    (1452, "2030-12-26", 29),
    (1453, "2031-12-16", 29),
    (1454, "2032-12-04", 30),
    (1455, "2033-11-23", 30),
    (1456, "2034-11-12", 30),
    (1457, "2035-11-01", 30),
    (1458, "2036-10-21", 29),
    (1459, "2037-10-10", 30),
    (1460, "2038-09-30", 29),
    (1461, "2039-09-19", 30),
    (1462, "2040-09-08", 29),
    (1463, "2041-08-28", 30),
    (1464, "2042-08-17", 30),
    (1465, "2043-08-06", 30),
    (1466, "2044-07-26", 29),
    (1467, "2045-07-16", 29),
    (1468, "2046-07-05", 30),
    (1469, "2047-06-25", 29),
    (1470, "2048-06-13", 30),
    (1471, "2049-06-02", 30),
    (1472, "2050-05-22", 30),
    (1473, "2051-05-12", 29),
    (1474, "2052-04-30", 30),
    (1475, "2053-04-20", 29),
    (1476, "2054-04-09", 30),
    (1477, "2055-03-30", 29),
    (1478, "2056-03-18", 29),
    (1479, "2057-03-07", 29),
    (1480, "2058-02-24", 30),
    (1481, "2059-02-14", 29),
    (1482, "2060-02-04", 29),
    (1483, "2061-01-23", 30),
    (1484, "2062-01-12", 30),
    (1485, "2063-01-01", 30),
    (1486, "2063-12-21", 30),
    (1487, "2064-12-10", 29),
    (1488, "2065-11-29", 30),
    (1489, "2066-11-19", 29),
    (1490, "2067-11-09", 29),
    (1491, "2068-10-28", 29),
    (1492, "2069-10-17", 29),
    (1493, "2070-10-06", 29),
    (1494, "2071-09-25", 29),
    (1495, "2072-09-14", 29),
    (1496, "2073-09-03", 30),
    (1497, "2074-08-24", 29),
    (1498, "2075-08-13", 30),
    (1499, "2076-08-01", 30),
    (1500, "2077-07-21", 30),
)


class RamadanMonth(NamedTuple):
    """One Ramadan: its Hijri year and its first and last local dates."""

    year: int
    first: int
    last: int

    @property
    def days(self) -> int:
        return self.last - self.first + 1


def _day(iso: str) -> int:
    return date.fromisoformat(iso).toordinal() - date(1970, 1, 1).toordinal()


MONTHS = tuple(
    RamadanMonth(year, _day(first), _day(first) + length - 1)
    for year, first, length in _RAMADAN
)
_FIRSTS = [month.first for month in MONTHS]
FIRST_YEAR = MONTHS[0].year
LAST_YEAR = MONTHS[-1].year


def ramadan_of(day: int) -> RamadanMonth | None:
    """The Ramadan the local date falls in, or None."""
    index = bisect_right(_FIRSTS, day) - 1
    if index >= 0 and day <= MONTHS[index].last:
        return MONTHS[index]
    return None


def ramadans_before(day: int) -> Iterator[RamadanMonth]:
    """Every Ramadan that began before the local date, newest first."""
    for index in range(bisect_right(_FIRSTS, day - 1) - 1, -1, -1):
        yield MONTHS[index]


def next_ramadan(day: int) -> RamadanMonth | None:
    """The Ramadan the date falls in, else the next one (None past the table)."""
    current = ramadan_of(day)
    if current is not None:
        return current
    index = bisect_right(_FIRSTS, day)
    return MONTHS[index] if index < len(MONTHS) else None
