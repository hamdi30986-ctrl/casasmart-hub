"""The activity log alone must not move NOW or the suggestions.

The golden NOW room_activity pins run again over a full log, and the whole
NOW reply and the generated room suggestions are compared, as JSON text,
with an empty log and with a full one. Real-HA environment only, like the
golden suite.

The NOW page reads the log through the recorder
(runtime.room_activity) to order off rooms that just went dark; these hubs
have none, so the table alone still moves nothing. The all-off ranking suites
(test_room_off_ranking*) cover the log through the recorder.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_now_room_activity_golden as golden
import view_harness as H

try:
    from casasmart.suggestion_runtime import SuggestionRuntime
    from casasmart.suggestion_store import SuggestionStore

    IMPORT_ERROR = golden.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


def fill_log(storage, member_ids):
    """Every room busy, from every source, with offs a minute ago."""
    now = int(time.time())
    events = []
    for index, (eid, _state, room_id, _attrs) in enumerate(golden.STATES):
        for age, action, source in (
            (86400 * 3, "on", "wall_switch"),
            (86400 * 2, "off", "automation"),
            (3600, "on", "app"),
            (600, "on", "ha_user"),
            (60 + index, "off", "scene"),
            (30, "off", "unknown"),
        ):
            events.append(
                {
                    "t": now - age,
                    "utc_offset_min": 180,
                    "room_id": room_id,
                    "entity_id": eid,
                    "action": action,
                    "source": source,
                    "member_id": member_ids[index % len(member_ids)]
                    if source in ("app", "scene")
                    else None,
                }
            )
    storage.room_activity().append_many(events)
    return len(events)


class NowGoldenOverAFullLog(golden.NowRoomActivityGoldenTest):
    """Every golden room_activity pin, with the log full of activity."""

    def setUp(self):
        super().setUp()
        fill_log(self.runtime.storage, ["mem-a", "mem-b"])


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class WholeRepliesUnchanged(golden.NowRoomActivityGoldenTest):
    def setUp(self):
        super().setUp()
        fixed = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
        self.runtime.suggestions = SuggestionRuntime(
            self.hass,
            SuggestionStore(self.runtime.storage),
            self.runtime.registry,
            now_data=self.now_data,
            clock=lambda: fixed,
        )
        self.now_data.set_room_policy("room-c", True, ["light.c_lamp"])
        self.now_data.save_restore_set("room-c", ["light.c_lamp"])

    async def replies(self, *, rooms=None, role="admin"):
        _, headers = H.session(self.runtime.auth, role=role, rooms=rooms)
        status, body = H.read_response(
            await self.view.get(H.FakeRequest(headers=headers))
        )
        self.assertEqual(status, 200)
        body.pop("generated_at")  # the time of the request
        generated = await self.runtime.suggestions.generated.payload("mem-a", rooms)
        return json.dumps(body), json.dumps(generated)

    async def test_now_and_generated_suggestions_are_byte_identical(self):
        cases = ({}, {"rooms": ["room-c", "room-e"], "role": "user"})
        before = [await self.replies(**case) for case in cases]
        self.assertGreater(fill_log(self.runtime.storage, ["mem-a", "mem-b"]), 0)
        after = [await self.replies(**case) for case in cases]
        self.assertEqual(after, before)
        # The generated block really is there to compare.
        self.assertIn('"source": "generated_room_v1"', before[0][1])

    # The inherited golden tests already run in NowGoldenOverAFullLog.
    test_without_policies = None
    test_with_policies_and_a_capture = None
    test_scoped_member = None
    test_more_than_four_rooms_feature_the_first = None


if __name__ == "__main__":
    unittest.main()
