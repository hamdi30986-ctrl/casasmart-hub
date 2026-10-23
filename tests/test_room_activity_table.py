"""RoomActivityTable: batched writes, the read API the ranking phases use, pruning."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from storage import HubStorage

# 2026-10-23 00:00:00 UTC, a Friday; 03:00 in Riyadh (UTC+3).
FRI_00_UTC = 1792713600
RIYADH = 180


def event(t, room="majlis", entity="light.majlis", action="on", **extra):
    return {
        "t": t,
        "utc_offset_min": RIYADH,
        "room_id": room,
        "entity_id": entity,
        "action": action,
        "source": extra.pop("source", "wall_switch"),
        **extra,
    }


class RoomActivityTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.table = self.storage.room_activity()

    # -- writes ----------------------------------------------------------------

    def test_append_many_writes_every_field(self):
        self.assertEqual(
            self.table.append_many(
                [
                    event(FRI_00_UTC, source="app", member_id="mem-a"),
                    event(FRI_00_UTC + 5, action="off"),
                ]
            ),
            2,
        )
        self.assertEqual(
            self.table.events(),
            [
                {
                    "id": 2,
                    "t": FRI_00_UTC + 5,
                    "utc_offset_min": RIYADH,
                    "room_id": "majlis",
                    "entity_id": "light.majlis",
                    "action": "off",
                    "source": "wall_switch",
                    "member_id": None,
                },
                {
                    "id": 1,
                    "t": FRI_00_UTC,
                    "utc_offset_min": RIYADH,
                    "room_id": "majlis",
                    "entity_id": "light.majlis",
                    "action": "on",
                    "source": "app",
                    "member_id": "mem-a",
                },
            ],
        )

    def test_a_bad_event_writes_none_of_the_batch(self):
        for bad in (
            {"t": -1},
            {"t": True},
            {"utc_offset_min": 1440},
            {"utc_offset_min": "180"},
            {"room_id": ""},
            {"entity_id": None},
            {"action": "x" * 33},
            {"member_id": ""},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.table.append_many([event(FRI_00_UTC), {**event(1), **bad}])
        self.assertEqual(self.table.count(), 0)
        self.assertEqual(self.table.append_many([]), 0)

    def test_rows_survive_a_reopen(self):
        self.table.append_many([event(FRI_00_UTC)])
        self.storage.close()
        reopened = HubStorage(Path(self._tmp.name) / "hub.db")
        reopened.open()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.room_activity().count(), 1)

    # -- room_hour_counts ------------------------------------------------------

    def test_hour_counts_use_the_local_day_hour_and_weekday(self):
        self.table.append_many(
            [
                # 20:30 UTC Thursday = 23:30 Thursday in Riyadh.
                event(FRI_00_UTC - 3 * 3600 - 1800),
                # 21:10 UTC Thursday = 00:10 Friday in Riyadh, twice.
                event(FRI_00_UTC - 3 * 3600 + 600),
                event(FRI_00_UTC - 3 * 3600 + 700, action="off"),
                event(FRI_00_UTC, room="kitchen", entity="light.kitchen"),
            ]
        )
        self.assertEqual(
            self.table.room_hour_counts(since_t=0),
            [
                {
                    "room_id": "kitchen",
                    "local_date": "2026-10-23",
                    "local_dow": 4,
                    "local_hour": 3,
                    "events": 1,
                },
                {
                    "room_id": "majlis",
                    "local_date": "2026-10-22",
                    "local_dow": 3,
                    "local_hour": 23,
                    "events": 1,
                },
                {
                    "room_id": "majlis",
                    "local_date": "2026-10-23",
                    "local_dow": 4,
                    "local_hour": 0,
                    "events": 2,
                },
            ],
        )

    def test_the_offset_stored_with_each_row_wins(self):
        # The hub moved from UTC to Riyadh: each row keeps the zone it was
        # recorded in.
        self.table.append_many(
            [
                {**event(FRI_00_UTC), "utc_offset_min": 0},
                event(FRI_00_UTC + 60),
            ]
        )
        hours = [row["local_hour"] for row in self.table.room_hour_counts(since_t=0)]
        self.assertEqual(hours, [0, 3])

    def test_hour_count_filters(self):
        self.table.append_many(
            [
                event(FRI_00_UTC, source="app", member_id="mem-a"),
                event(FRI_00_UTC + 1, source="app", member_id="mem-b"),
                event(FRI_00_UTC + 2, action="off", source="app", member_id="mem-a"),
                event(FRI_00_UTC + 3, source="automation"),
                event(FRI_00_UTC + 4, room="kitchen", entity="light.kitchen"),
                event(FRI_00_UTC + 7200),
            ]
        )

        def total(**filters):
            rows = self.table.room_hour_counts(**filters)
            return sum(row["events"] for row in rows)

        self.assertEqual(total(since_t=FRI_00_UTC), 6)
        self.assertEqual(total(since_t=FRI_00_UTC + 1), 5)
        self.assertEqual(total(since_t=0, until_t=FRI_00_UTC + 4), 4)
        self.assertEqual(total(since_t=0, member_id="mem-a"), 2)
        self.assertEqual(total(since_t=0, member_id="mem-a", actions=["on"]), 1)
        self.assertEqual(total(since_t=0, sources=["app", "wall_switch"]), 5)
        self.assertEqual(total(since_t=0, room_ids=["kitchen"]), 1)
        self.assertEqual(total(since_t=0, room_ids=[]), 0)
        self.assertEqual(total(since_t=0, room_ids=["gone"]), 0)

    def test_bad_filters_are_refused(self):
        for filters in (
            {"since_t": -1},
            {"since_t": 0, "until_t": "x"},
            {"since_t": 0, "room_ids": "majlis"},
            {"since_t": 0, "member_id": ""},
            {"since_t": 0, "actions": [None]},
        ):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.table.room_hour_counts(**filters)

    # -- events / last_off_by_room ---------------------------------------------

    def test_events_for_a_member_or_room_newest_first(self):
        self.table.append_many(
            [
                event(FRI_00_UTC, source="app", member_id="mem-a"),
                event(FRI_00_UTC, room="kitchen", source="app", member_id="mem-a"),
                event(FRI_00_UTC + 1, source="app", member_id="mem-b"),
                event(FRI_00_UTC + 2),
            ]
        )
        mine = self.table.events(member_id="mem-a")
        self.assertEqual([row["id"] for row in mine], [2, 1])
        self.assertEqual(
            [row["id"] for row in self.table.events(room_id="majlis", limit=2)],
            [4, 3],
        )
        self.assertEqual(
            [row["id"] for row in self.table.events(since_t=FRI_00_UTC + 1)], [4, 3]
        )
        for limit in (0, 5001, True):
            with self.assertRaises(ValueError):
                self.table.events(limit=limit)

    def test_last_off_by_room(self):
        self.table.append_many(
            [
                event(FRI_00_UTC, action="off", source="app", member_id="mem-a"),
                event(FRI_00_UTC + 10, action="off", entity="light.majlis_spot"),
                event(FRI_00_UTC + 20, action="on"),
                event(FRI_00_UTC + 5, room="kitchen", action="off", source="scene"),
                event(FRI_00_UTC + 9, room="bath", action="on"),
            ]
        )
        self.assertEqual(
            self.table.last_off_by_room(["majlis", "kitchen", "bath", "gone"]),
            {
                "majlis": {
                    "t": FRI_00_UTC + 10,
                    "entity_id": "light.majlis_spot",
                    "source": "wall_switch",
                    "member_id": None,
                },
                "kitchen": {
                    "t": FRI_00_UTC + 5,
                    "entity_id": "light.majlis",
                    "source": "scene",
                    "member_id": None,
                },
            },
        )
        self.assertEqual(
            list(
                self.table.last_off_by_room(
                    ["majlis", "kitchen"], since_t=FRI_00_UTC + 6
                )
            ),
            ["majlis"],
        )
        self.assertEqual(self.table.last_off_by_room([]), {})

    def test_same_second_ties_go_to_the_later_row(self):
        self.table.append_many(
            [
                event(FRI_00_UTC, action="off", entity="light.a"),
                event(FRI_00_UTC, action="off", entity="light.b"),
            ]
        )
        self.assertEqual(
            self.table.last_off_by_room(["majlis"])["majlis"]["entity_id"], "light.b"
        )

    # -- pruning ---------------------------------------------------------------

    def test_prune_by_age_and_by_source(self):
        self.table.append_many(
            [
                event(100, source="automation"),
                event(100),
                event(200, source="automation"),
                event(300),
            ]
        )
        self.assertEqual(self.table.prune(before_t=250, sources=["automation"]), 2)
        self.assertEqual([row["t"] for row in self.table.events()], [300, 100])
        self.assertEqual(self.table.prune(before_t=250), 1)
        self.assertEqual(self.table.count(), 1)
        with self.assertRaises(ValueError):
            self.table.prune(before_t=0, sources=[])

    def test_prune_runs_in_batches(self):
        self.table._DELETE_BATCH = 3
        self.table.append_many([event(t) for t in range(10)])
        self.assertEqual(self.table.prune(before_t=8), 8)
        self.assertEqual(self.table.count(), 2)

    def test_trim_to_drops_the_oldest(self):
        self.table._DELETE_BATCH = 4
        self.table.append_many([event(t) for t in (5, 1, 9, 3, 7, 2, 8)])
        self.assertEqual(self.table.trim_to(10), 0)
        self.assertEqual(self.table.trim_to(3), 4)
        self.assertEqual(sorted(row["t"] for row in self.table.events()), [7, 8, 9])
        self.assertEqual(self.table.trim_to(0), 3)
        self.assertEqual(self.table.count(), 0)

    def test_delete_member_and_clear(self):
        self.table.append_many(
            [
                event(1, source="app", member_id="mem-a"),
                event(2, source="app", member_id="mem-b"),
                event(3),
            ]
        )
        self.assertEqual(self.table.delete_member("mem-a"), 1)
        self.assertEqual(
            [row["member_id"] for row in self.table.events()], [None, "mem-b"]
        )
        self.table.clear()
        self.assertEqual(self.table.count(), 0)

    # -- query plans -----------------------------------------------------------

    def test_room_and_member_queries_use_their_indexes(self):
        conn = sqlite3.connect(Path(self._tmp.name) / "hub.db")
        self.addCleanup(conn.close)

        def plan(sql, params):
            return " ".join(
                row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            )

        self.assertIn(
            "idx_room_activity_room_t",
            plan(
                "SELECT * FROM room_activity_events WHERE room_id = ? AND t >= ?",
                ("majlis", 0),
            ),
        )
        self.assertIn(
            "idx_room_activity_member_t",
            plan(
                "SELECT * FROM room_activity_events WHERE member_id = ? AND t >= ?",
                ("mem-a", 0),
            ),
        )
        self.assertIn(
            "idx_room_activity_t",
            plan("SELECT id FROM room_activity_events WHERE t < ? ORDER BY t", (5,)),
        )


if __name__ == "__main__":
    unittest.main()
