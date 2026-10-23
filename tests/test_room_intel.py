"""room_intel wiring: the Now page and generated suggestions share one ranking.

Real-HA environment only (it imports the views and runtimes as the hub does).
The ranking's answers themselves are pinned in test_room_ranking_golden and
test_now_room_activity_golden.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart import now_api, suggestion_runtime
    from casasmart.room_intel import ranking

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class SharedRankingTest(unittest.TestCase):
    def test_now_and_generated_suggestions_call_the_same_ranking(self):
        self.assertIs(now_api.async_rank_rooms, ranking.async_rank_rooms)
        self.assertIs(suggestion_runtime.async_rank_rooms, ranking.async_rank_rooms)

    def test_now_no_longer_ranks_through_the_suggestion_runtime(self):
        source = (
            Path(__file__).parents[1] / "custom_components/casasmart/now_api.py"
        ).read_text()
        self.assertNotIn("room_context", source)
        self.assertNotIn(".generated", source)


if __name__ == "__main__":
    unittest.main()
