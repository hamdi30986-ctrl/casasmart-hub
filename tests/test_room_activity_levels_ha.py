"""The recorder logs levels: what a device was switched on or set to, and a
person's level changes while it stays on.

The real ActivityRecorder over a real Home Assistant core and a temp
HubStorage (the fixture of test_room_activity_recorder). States are written
as an entity writes them: in the context of the call that changed them, or
in a fresh one when the device reports by itself. The recorder's clock is a
fake, to step past LEVEL_FOLLOW_SECONDS. Real-HA environment only.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_room_activity_recorder import IMPORT_ERROR, RecorderTestCase

try:
    from casasmart.room_intel import activity
    from homeassistant.const import EVENT_CALL_SERVICE
    from homeassistant.core import Context
except Exception:  # reported by the skip below
    pass

APP = "app"


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class LevelCase(RecorderTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mono = 1000.0
        self.recorder._clock = lambda: self.mono

    def report(self, entity_id, state, attributes=None, context=None):
        """The entity writes its state (a fresh context: the device itself)."""
        self.hass.states.async_set(entity_id, state, attributes, context=context)

    def app(self, member="mem-a"):
        return self.recorder.command_context(activity.SOURCE_APP, member)

    async def levels(self):
        await self.hass.async_block_till_done()
        await self.recorder.async_flush()
        return [
            (e["entity_id"], e["action"], e["source"], e["member_id"], e["level"])
            for e in reversed(self.storage.room_activity().events(limit=1000))
        ]


class SwitchOnLevelTests(LevelCase):
    async def test_an_on_carries_the_level_it_came_on_at(self):
        self.report(
            "light.majlis", "on", {"brightness": 200, "color_mode": "brightness"}
        )
        self.report("climate.majlis_ac", "cool", {"temperature": 22, "fan_mode": "low"})
        self.report("climate.majlis_ac", "dry", {"temperature": 22, "fan_mode": "low"})
        self.report("switch.kitchen_left", "on")
        self.report("light.majlis", "off", {"brightness": None})
        self.assertEqual(
            await self.levels(),
            [
                ("light.majlis", "on", "wall_switch", None, {"brightness": 200}),
                ("climate.majlis_ac", "on", "wall_switch", None,
                 {"hvac_mode": "cool", "temperature": 22.0, "fan_mode": "low"}),
                ("climate.majlis_ac", "set", "wall_switch", None,
                 {"hvac_mode": "dry", "temperature": 22.0, "fan_mode": "low"}),
                ("switch.kitchen_left", "on", "wall_switch", None, None),
                ("light.majlis", "off", "wall_switch", None, None),
            ],
        )  # fmt: skip

    async def test_a_change_before_the_row_is_written_becomes_its_level(self):
        # Switched on, then dimmed at once: one row, at the level it settled.
        context = self.app()
        self.report("light.majlis", "on", {"brightness": 255}, context)
        self.report("light.majlis", "on", {"brightness": 60}, context)
        self.report("light.majlis", "on", {"brightness": 64})  # the bulb's report
        self.assertEqual(
            await self.levels(),
            [("light.majlis", "on", APP, "mem-a", {"brightness": 64})],
        )


class LevelChangeTests(LevelCase):
    async def test_a_level_changed_later_is_a_level_row(self):
        self.report("light.majlis", "on", {"brightness": 200})
        self.report(
            "climate.majlis_ac", "cool", {"temperature": 24, "fan_mode": "auto"}
        )
        await self.levels()
        self.mono += 600
        self.report("light.majlis", "on", {"brightness": 90})
        self.report(
            "climate.majlis_ac", "cool", {"temperature": 22, "fan_mode": "auto"},
            Context(user_id="ha-user-1"),
        )  # fmt: skip
        # The room getting cooler is no change of level.
        self.report(
            "climate.majlis_ac",
            "cool",
            {"temperature": 22, "fan_mode": "auto", "current_temperature": 25},
        )
        self.assertEqual(
            (await self.levels())[2:],
            [
                ("light.majlis", "level", "wall_switch", None, {"brightness": 90}),
                ("climate.majlis_ac", "level", "ha_user", None,
                 {"hvac_mode": "cool", "temperature": 22.0, "fan_mode": "auto"}),
            ],
        )  # fmt: skip

    async def test_a_slider_dragged_before_a_write_is_one_row(self):
        self.report("light.majlis", "on", {"brightness": 200})
        await self.levels()
        self.mono += 600
        context = self.app()
        for brightness in (180, 150, 120, 100):
            self.report("light.majlis", "on", {"brightness": brightness}, context)
        self.assertEqual(
            (await self.levels())[1:],
            [("light.majlis", "level", APP, "mem-a", {"brightness": 100})],
        )

    async def test_the_devices_own_report_after_a_switch_on_is_that_switch_ons(self):
        self.report("light.majlis", "on", {"brightness": 10}, self.app())
        await self.levels()
        # The light ramps up and reports by itself, 2 s later.
        self.mono += 2
        self.report("light.majlis", "on", {"brightness": 180})
        # Long after: a wall dimmer.
        self.mono += activity.LEVEL_FOLLOW_SECONDS
        self.report("light.majlis", "on", {"brightness": 40})
        self.assertEqual(
            await self.levels(),
            [
                ("light.majlis", "on", APP, "mem-a", {"brightness": 10}),
                ("light.majlis", "level", APP, "mem-a", {"brightness": 180}),
                ("light.majlis", "level", "wall_switch", None, {"brightness": 40}),
            ],
        )

    async def test_a_change_by_someone_else_is_not_merged(self):
        self.report("light.majlis", "on", {"brightness": 200}, self.app())
        self.report("light.majlis", "on", {"brightness": 30}, Context(user_id="u1"))
        self.assertEqual(
            await self.levels(),
            [
                ("light.majlis", "on", APP, "mem-a", {"brightness": 200}),
                ("light.majlis", "level", "ha_user", None, {"brightness": 30}),
            ],
        )

    async def test_no_level_row_from_an_automation_or_an_unknown_caller(self):
        self.report("light.majlis", "on", {"brightness": 200})
        await self.levels()
        self.mono += 600
        # An automation dims it; the bulb's report that follows is the
        # automation's too.
        run = Context()
        self.hass.bus.async_fire("automation_triggered", {}, context=run)
        self.report("light.majlis", "on", {"brightness": 50}, run)
        # Another integration's service call (HomeKit).
        call = Context()
        self.hass.bus.async_fire(EVENT_CALL_SERVICE, {}, context=call)
        self.report("light.majlis", "on", {"brightness": 70}, call)
        # An automation switches the spot on; the spot ramps up by itself.
        timed = Context()
        self.hass.bus.async_fire("automation_triggered", {}, context=timed)
        self.report("light.majlis_spot", "on", {"brightness": 5}, timed)
        await self.levels()
        self.mono += 3
        self.report("light.majlis_spot", "on", {"brightness": 240})
        self.assertEqual(
            (await self.levels())[1:],
            [("light.majlis_spot", "on", "automation", None, {"brightness": 5})],
        )

    async def test_level_rows_have_their_own_cap(self):
        self.report("light.majlis", "on", {"brightness": 1})
        self.mono += 600
        for i in range(30):
            await self.levels()  # written each time: nothing to merge into
            self.report("light.majlis", "on", {"brightness": 100 + i})
        rows = await self.levels()
        self.assertEqual(len(rows), 1 + 20)
        # The light's on and off still get through.
        self.report("light.majlis", "off")
        self.assertEqual((await self.levels())[-1][:2], ("light.majlis", "off"))

    async def test_level_rows_never_count_as_a_switch_for_just_off(self):
        self.report("light.majlis", "on", {"brightness": 200})
        await self.levels()
        self.mono += 600
        self.report("light.majlis", "on", {"brightness": 20})  # queued
        recent = await self.recorder.async_recent_events(0, ["majlis"])
        self.assertEqual([e["action"] for e in recent], ["on"])


class ForgetTests(LevelCase):
    async def test_a_deleted_members_switch_on_takes_no_later_report(self):
        self.report("light.majlis", "on", {"brightness": 10}, self.app())
        await self.recorder.async_forget_member("mem-a")
        self.mono += 2
        self.report("light.majlis", "on", {"brightness": 180})
        self.assertEqual(
            await self.levels(),
            [("light.majlis", "level", "wall_switch", None, {"brightness": 180})],
        )


if __name__ == "__main__":
    unittest.main()
