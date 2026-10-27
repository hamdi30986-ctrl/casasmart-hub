"""The room activity log's level columns (v6): written, checked and read.

storage.RoomActivityTable over real SQLite; the rules that make a level are
in test_room_activity_rules. Nothing here needs Home Assistant.
"""

import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from room_intel import activity
from storage import HubStorage

# 2026-10-23 00:00:00 UTC, a Friday; 03:00 in Riyadh (UTC+3).
FRI_00_UTC = 1792713600
RIYADH = 180
HUMAN = ["app", "scene", "ha_user", "wall_switch"]


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


def local(hour, minute=0, day=0):
    """Friday + day at hour:minute in Riyadh, as epoch seconds."""
    return FRI_00_UTC + day * 86400 + (hour - 3) * 3600 + minute * 60


class LevelColumnsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.table = self.storage.room_activity()

    def test_a_level_is_written_and_read_back(self):
        self.table.append_many(
            [
                event(local(19), level={"brightness": 204, "color_temp_kelvin": 2700}),
                event(
                    local(19, 1),
                    entity="climate.majlis",
                    level={"hvac_mode": "cool", "temperature": 23, "fan_mode": "low"},
                ),
                event(local(19, 2), entity="fan.majlis", level={"percentage": 40}),
                event(local(19, 3), action="off"),
                event(local(19, 4), level={}),
            ]
        )
        self.assertEqual(
            [(e["entity_id"], e["level"]) for e in reversed(self.table.events())],
            [
                ("light.majlis", {"brightness": 204, "color_temp_kelvin": 2700}),
                (
                    "climate.majlis",
                    {"hvac_mode": "cool", "temperature": 23.0, "fan_mode": "low"},
                ),
                ("fan.majlis", {"percentage": 40}),
                ("light.majlis", None),
                ("light.majlis", None),
            ],
        )

    def test_a_bad_level_writes_none_of_the_batch(self):
        for bad in (
            [],
            "brightness=3",
            {"brightness": 0},
            {"brightness": 256},
            {"brightness": 12.5},
            {"brightness": True},
            {"percentage": 101},
            {"color_temp_kelvin": 0},
            {"temperature": math.nan},
            {"temperature": math.inf},
            {"temperature": "23"},
            {"hvac_mode": ""},
            {"fan_mode": "x" * 65},
            {"rgb_color": [1, 2, 3]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.table.append_many([event(local(9)), event(local(9), level=bad)])
        self.assertEqual(self.table.count(), 0)

    def test_level_rows_read_newest_first_at_the_asked_hours(self):
        self.table.append_many(
            [
                event(local(19, 5, -7), level={"brightness": 100}),
                event(local(19, 50, -1), level={"brightness": 150}),
                event(local(20, 10), level={"brightness": 30}),  # another hour
                event(local(18, 55), level={"brightness": 60}),
                event(local(19, 10), action="off"),  # no level
                event(local(19, 20), source="automation", level={"brightness": 9}),
                event(local(19, 30), room="kitchen", level={"brightness": 7}),
                event(
                    local(19, 40), entity="light.majlis_spot", level={"brightness": 8}
                ),
                event(local(19, 45), action="level", level={"brightness": 200}),
            ]
        )

        def read(**kwargs):
            rows = self.table.level_rows(["majlis"], **kwargs)
            return [(e["t"], e["action"], e["level"]["brightness"]) for e in rows]

        self.assertEqual(
            read(
                since_t=0, local_hours=[19], sources=HUMAN, entity_ids=["light.majlis"]
            ),
            [
                (local(19, 45), "level", 200),
                (local(19, 50, -1), "on", 150),
                (local(19, 5, -7), "on", 100),
            ],
        )
        self.assertEqual(
            read(
                since_t=local(0, 0, -2),
                local_hours=[18, 19, 20],
                sources=HUMAN,
                entity_ids=["light.majlis"],
            ),
            [
                (local(20, 10), "on", 30),
                (local(19, 45), "level", 200),
                (local(18, 55), "on", 60),
                (local(19, 50, -1), "on", 150),
            ],
        )
        # Every device in the room; only these sources; none for no hours.
        self.assertEqual(len(read(since_t=0, local_hours=[19], sources=HUMAN)), 4)
        self.assertEqual(
            read(since_t=0, local_hours=[19], sources=["automation"]),
            [(local(19, 20), "on", 9)],
        )
        self.assertEqual(read(since_t=0, local_hours=[], sources=HUMAN), [])
        self.assertEqual(
            read(since_t=0, local_hours=[19], sources=HUMAN, entity_ids=[]), []
        )
        for bad in (
            {"local_hours": [24]},
            {"local_hours": [True]},
            {"since_t": -1},
            {"sources": "app"},
            {"limit": 0},
            {"limit": 5001},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.table.level_rows(
                    ["majlis"],
                    **{"since_t": 0, "local_hours": [19], "sources": HUMAN, **bad},
                )

    def test_level_rows_use_the_room_index(self):
        seen = []
        fetchall = self.storage._fetchall

        def capture(sql, params=()):
            seen.append((sql, params))
            return fetchall(sql, params)

        self.storage._fetchall = capture
        self.table.level_rows(
            ["majlis"],
            since_t=0,
            local_hours=[18, 19, 20],
            sources=HUMAN,
            entity_ids=["light.majlis"],
        )
        ((sql, params),) = seen
        plan = " ".join(
            row[3]
            for row in self.storage._connection.execute(
                f"EXPLAIN QUERY PLAN {sql}", params
            )
        )
        self.assertIn(
            "SEARCH room_activity_events USING INDEX idx_room_activity_room_t", plan
        )

    def test_a_level_change_alone_is_no_habit_activity(self):
        fri = (FRI_00_UTC + RIYADH * 60) // 86400
        self.table.append_many(
            [
                # Friday: only dimmed a light left on since Thursday.
                event(local(20), action="level", level={"brightness": 40}),
                # Saturday: switched on at 19:00 with its level.
                event(local(19, 0, 1), level={"brightness": 200}),
            ]
        )
        rows = self.table.habit_rows(first_day=fri, last_day=fri + 1, sources=HUMAN)
        self.assertEqual(
            [(r["local_day"], r["on_hours"]) for r in rows], [(fri + 1, 1 << 19)]
        )

    def test_retention_takes_level_rows_like_any_other(self):
        now = local(12)
        day = 86400
        self.table.append_many(
            [
                event(now - 401 * day, action="level", level={"brightness": 1}),
                event(now - 399 * day, action="level", level={"brightness": 2}),
                event(now - 1 * day, level={"brightness": 3}),
            ]
        )
        self.assertEqual(
            activity.prune(self.table, now), {"automation": 0, "age": 1, "cap": 0}
        )
        self.assertEqual(
            [e["level"]["brightness"] for e in self.table.events()], [3, 2]
        )
        self.assertEqual(self.table.trim_to(1), 1)
        self.assertEqual([e["level"] for e in self.table.events()], [{"brightness": 3}])


if __name__ == "__main__":
    unittest.main()
