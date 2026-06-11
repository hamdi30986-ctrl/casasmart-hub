"""Unit tests for the history query contract (B16 stage 3c-3).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import the module directly — the casasmart package __init__ imports
# homeassistant, which isn't installed in the test environment.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "casasmart")
)

from history import (  # noqa: E402
    HistoryQueryError,
    MAX_HISTORY_ENTITIES,
    MAX_HISTORY_RANGE,
    parse_history_query,
    serialize_history,
    serialize_history_point,
)

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


class FakeState:
    """Duck-typed stand-in for homeassistant.core.State."""

    def __init__(self, state, last_changed):
        self.state = state
        self.last_changed = last_changed


class TestParseHistoryQuery(unittest.TestCase):
    def parse(self, **params):
        return parse_history_query(params, now=NOW)

    def test_happy_path(self):
        ids, start, end, significant = self.parse(
            entities="sensor.a_energy_month, sensor.b_energy_month",
            start="2026-06-10T12:00:00+00:00",
            end="2026-06-10T13:00:00+00:00",
            significant="0",
        )
        self.assertEqual(ids, ["sensor.a_energy_month", "sensor.b_energy_month"])
        self.assertEqual(start, datetime(2026, 6, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 6, 10, 13, tzinfo=timezone.utc))
        self.assertFalse(significant)

    def test_end_defaults_to_now(self):
        _, _, end, significant = self.parse(
            entities="sensor.a", start="2026-06-11T11:00:00+00:00"
        )
        self.assertEqual(end, NOW)
        self.assertTrue(significant)  # default is significant-only

    def test_future_end_clamped_to_now(self):
        _, _, end, _ = self.parse(
            entities="sensor.a",
            start="2026-06-11T11:00:00+00:00",
            end="2026-06-12T11:00:00+00:00",
        )
        self.assertEqual(end, NOW)

    def test_offset_timestamps_normalized_to_utc(self):
        _, start, _, _ = self.parse(
            entities="sensor.a", start="2026-06-11T13:00:00+03:00"
        )
        self.assertEqual(start, datetime(2026, 6, 11, 10, tzinfo=timezone.utc))

    def test_missing_entities_rejected(self):
        for params in ({}, {"entities": ""}, {"entities": " , ,"}):
            with self.assertRaises(HistoryQueryError):
                parse_history_query(
                    {**params, "start": "2026-06-11T11:00:00+00:00"}, now=NOW
                )

    def test_malformed_entity_id_rejected(self):
        for bad in ("sensor.", ".energy", "notadomain", "a.b,c"):
            with self.assertRaises(HistoryQueryError):
                self.parse(entities=bad, start="2026-06-11T11:00:00+00:00")

    def test_too_many_entities_rejected(self):
        ids = ",".join(f"sensor.e{i}" for i in range(MAX_HISTORY_ENTITIES + 1))
        with self.assertRaises(HistoryQueryError):
            self.parse(entities=ids, start="2026-06-11T11:00:00+00:00")

    def test_entity_count_at_cap_allowed(self):
        ids = ",".join(f"sensor.e{i}" for i in range(MAX_HISTORY_ENTITIES))
        parsed, _, _, _ = self.parse(
            entities=ids, start="2026-06-11T11:00:00+00:00"
        )
        self.assertEqual(len(parsed), MAX_HISTORY_ENTITIES)

    def test_missing_start_rejected(self):
        with self.assertRaises(HistoryQueryError):
            self.parse(entities="sensor.a")

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(HistoryQueryError):
            self.parse(entities="sensor.a", start="2026-06-11T11:00:00")

    def test_garbage_timestamp_rejected(self):
        with self.assertRaises(HistoryQueryError):
            self.parse(entities="sensor.a", start="yesterday")

    def test_start_after_end_rejected(self):
        with self.assertRaises(HistoryQueryError):
            self.parse(
                entities="sensor.a",
                start="2026-06-11T11:00:00+00:00",
                end="2026-06-11T10:00:00+00:00",
            )

    def test_range_over_cap_rejected(self):
        start = NOW - MAX_HISTORY_RANGE - timedelta(seconds=1)
        with self.assertRaises(HistoryQueryError):
            self.parse(entities="sensor.a", start=start.isoformat())

    def test_range_at_cap_allowed(self):
        start = NOW - MAX_HISTORY_RANGE
        _, parsed_start, _, _ = self.parse(
            entities="sensor.a", start=start.isoformat()
        )
        self.assertEqual(parsed_start, start)


class TestSerializeHistoryPoint(unittest.TestCase):
    def test_state_object(self):
        ts = datetime(2026, 6, 10, 3, tzinfo=timezone.utc)
        self.assertEqual(
            serialize_history_point(FakeState("12.5", ts)),
            {"state": "12.5", "last_changed": ts.isoformat()},
        )

    def test_minimal_response_dict(self):
        # The recorder's minimal_response rows after the first are dicts.
        self.assertEqual(
            serialize_history_point(
                {"state": "13.0", "last_changed": "2026-06-10T04:00:00+00:00"}
            ),
            {"state": "13.0", "last_changed": "2026-06-10T04:00:00+00:00"},
        )

    def test_dict_with_datetime_timestamp(self):
        ts = datetime(2026, 6, 10, 5, tzinfo=timezone.utc)
        self.assertEqual(
            serialize_history_point({"state": "1", "last_changed": ts}),
            {"state": "1", "last_changed": ts.isoformat()},
        )

    def test_unusable_rows_dropped(self):
        for bad in (
            {"state": None, "last_changed": "2026-06-10T04:00:00+00:00"},
            {"state": "1"},
            {"last_changed": "2026-06-10T04:00:00+00:00"},
            FakeState(None, datetime(2026, 6, 10, tzinfo=timezone.utc)),
            FakeState("1", None),
            object(),
        ):
            self.assertIsNone(serialize_history_point(bad), msg=repr(bad))


class TestSerializeHistory(unittest.TestCase):
    def test_every_allowed_entity_gets_a_key(self):
        ts = datetime(2026, 6, 10, 3, tzinfo=timezone.utc)
        wire = serialize_history(
            ["sensor.a", "sensor.b"],
            {"sensor.a": [FakeState("1", ts)]},
        )
        self.assertEqual(
            wire,
            {
                "sensor.a": [{"state": "1", "last_changed": ts.isoformat()}],
                "sensor.b": [],
            },
        )

    def test_unrequested_recorder_keys_ignored(self):
        # Defense in depth: the wire only ever carries what was allowed.
        wire = serialize_history(["sensor.a"], {"sensor.other": [object()]})
        self.assertEqual(wire, {"sensor.a": []})

    def test_bad_rows_dropped_good_rows_kept(self):
        ts = datetime(2026, 6, 10, 3, tzinfo=timezone.utc)
        wire = serialize_history(
            ["sensor.a"],
            {"sensor.a": [object(), FakeState("2", ts), {"state": None}]},
        )
        self.assertEqual(
            wire, {"sensor.a": [{"state": "2", "last_changed": ts.isoformat()}]}
        )


if __name__ == "__main__":
    unittest.main()
