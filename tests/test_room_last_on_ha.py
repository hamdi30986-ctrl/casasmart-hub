"""Each room's last-on set, kept by the recorder, against a real Home Assistant.

A real HomeAssistant core (bus, state machine, services, Context, CoreState)
drives the real ActivityRecorder over a temp HubStorage and the real
RegistryEngine; only Home Assistant's entity registry and the config-entry
lookup are fakes (as in test_room_activity_recorder). The rules themselves are
pinned without Home Assistant in test_room_one_tap. LastOnHubCase is also the
fixture of test_room_one_tap_ha. Real-HA environment only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.const import EVENT_REGISTRY_CHANGED
    from casasmart.registry import RegistryEngine
    from casasmart.room_intel import activity
    from casasmart.room_intel.recorder import ActivityRecorder
    from casasmart.storage import HubStorage
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import Context, CoreState, HomeAssistant
    from homeassistant.helpers import entity_registry as er

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


class _NoEntityRegistry:
    """HA's entity registry with no entries: every entity is visible."""

    def async_get(self, entity_id):
        return None


ROOMS = {
    "majlis": {"name": "Majlis", "floor_id": None, "icon": None, "sort_order": 0},
    "kitchen": {"name": "Kitchen", "floor_id": None, "icon": None, "sort_order": 1},
    "bedroom": {"name": "Bedroom", "floor_id": None, "icon": None, "sort_order": 2},
}
SWITCH = {"device_class": "switch"}
# device id -> (room, [(entity id, state, attributes)], gangs)
DEVICES = {
    "dev-majlis": (
        "majlis",
        [("light.majlis_main", "off", {}), ("light.majlis_spot", "off", {})],
        {},
    ),
    "dev-majlis-fan": ("majlis", [("fan.majlis", "off", {})], {}),
    "dev-majlis-relay": (
        "majlis",
        [("switch.majlis_relay", "off", SWITCH)],
        {"switch.majlis_relay": {"type": "light"}},
    ),
    "dev-majlis-outlet": (
        "majlis",
        [("switch.majlis_outlet", "off", SWITCH)],
        {"switch.majlis_outlet": {"type": "outlet"}},
    ),
    "dev-majlis-ac": (
        "majlis",
        [("climate.majlis_ac", "off", {"hvac_modes": ["off", "cool"]})],
        {},
    ),
    "dev-majlis-curtain": ("majlis", [("cover.majlis_curtain", "closed", {})], {}),
    "dev-majlis-tv": ("majlis", [("media_player.majlis_tv", "off", {})], {}),
    "dev-majlis-lamp": (
        "majlis",
        [("light.majlis_lamp", "off", {"casasmart_room_activity_exclude": True})],
        {},
    ),
    "dev-kitchen": (
        "kitchen",
        [("light.kitchen", "off", {}), ("fan.kitchen_hood", "off", {})],
        {},
    ),
    "dev-kitchen-heater": (
        "kitchen",
        [("switch.kitchen_heater", "off", SWITCH)],
        {"switch.kitchen_heater": {"type": "heater"}},
    ),
    "dev-kitchen-tank": ("kitchen", [("water_heater.kitchen_tank", "off", {})], {}),
    "dev-bedroom": ("bedroom", [("light.bedroom", "off", {})], {}),
    # An HA area that isn't a registry room.
    "dev-hall": ("hall", [("light.hall", "off", {})], {}),
}
# In a registry room, but not imported into any user device.
UNIMPORTED = ("light.kitchen_unimported", "kitchen")
MAJLIS_LIGHTS = ("light.majlis_main", "light.majlis_spot", "fan.majlis")
_SERVICE_STATES = {"turn_on": "on", "turn_off": "off"}


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class LastOnHubCase(unittest.IsolatedAsyncioTestCase):
    """A hub over a real Home Assistant core, the recorder following lights."""

    already_running = True
    # The recorder's settle after Home Assistant starts (600 s on a hub).
    settle_seconds = 0.05

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = HubStorage(Path(self._tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        for room_id, record in ROOMS.items():
            self.storage.table("registry_rooms")[room_id] = record
        for device_id, (room_id, entities, gangs) in DEVICES.items():
            for eid, _, _ in entities:
                self.storage.table("registry_devices")[eid] = {
                    "room_id": room_id,
                    "display_name": None,
                    "sort_order": 0,
                }
            self.storage.table("registry_user_devices")[device_id] = {
                "entity_ids": [eid for eid, _, _ in entities],
                "gangs": gangs,
            }
        self.storage.table("registry_devices")[UNIMPORTED[0]] = {
            "room_id": UNIMPORTED[1],
            "display_name": None,
            "sort_order": 0,
        }
        self.registry = RegistryEngine(
            *(
                self.storage.table(name)
                for name in (
                    "registry_floors",
                    "registry_rooms",
                    "registry_devices",
                    "registry_scenes",
                    "registry_favorites",
                    "registry_user_devices",
                    "registry_room_tags",
                )
            )
        )
        self.registry.warm_up()

        self.hass = HomeAssistant(self._tmp.name)
        self.hass.config.time_zone = "Asia/Riyadh"
        self.hass.data[er.DATA_REGISTRY] = _NoEntityRegistry()
        self.runtime = types.SimpleNamespace(registry=self.registry)
        entry = types.SimpleNamespace(runtime_data=self.runtime)
        self.hass.config_entries = types.SimpleNamespace(
            async_loaded_entries=lambda domain: [entry]
        )
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        for domain in ("light", "fan", "switch", "water_heater"):
            for service in _SERVICE_STATES:
                self.hass.services.async_register(domain, service, self._handle)
        for _, (_, entities, _) in DEVICES.items():
            for eid, state, attributes in entities:
                self.hass.states.async_set(eid, state, attributes)
        self.hass.states.async_set(UNIMPORTED[0], "off")
        await self.hass.async_block_till_done()
        if self.already_running:
            self.hass.set_state(CoreState.running)
        self.recorder = self.new_recorder()
        await self.recorder.async_start()
        self.runtime.room_activity = self.recorder

    def new_recorder(self, **kwargs):
        return ActivityRecorder(
            self.hass,
            self.registry,
            self.storage.room_activity(),
            last_on_table=self.storage.table("now_room_last_on"),
            startup_settle=self.settle_seconds,
            **kwargs,
        )

    async def asyncTearDown(self):
        await self.recorder.async_stop()
        await self.hass.async_block_till_done()

    async def _handle(self, call):
        ids = call.data["entity_id"]
        ids = [ids] if isinstance(ids, str) else list(ids)
        self.calls.append((call.domain, call.service, tuple(ids)))
        for entity_id in ids:
            state = self.hass.states.get(entity_id)
            self.hass.states.async_set(
                entity_id,
                _SERVICE_STATES[call.service],
                state.attributes if state is not None else None,
                context=call.context,
            )

    async def call(self, domain, service, entity_ids, context=None):
        await self.hass.services.async_call(
            domain, service, {"entity_id": entity_ids}, blocking=True, context=context
        )
        await self.hass.async_block_till_done()

    def press(self, entity_id, state, *, at=None):
        """A device reporting a change by itself (a wall switch)."""
        current = self.hass.states.get(entity_id)
        self.hass.states.async_set(
            entity_id,
            state,
            current.attributes if current is not None else None,
            timestamp=at,
        )

    async def saved(self):
        """The last-on sets written to storage."""
        await self.hass.async_block_till_done()
        await self.recorder.async_flush()
        return dict(self.storage.table("now_room_last_on").items())

    def last_on(self, room_id):
        return self.recorder.last_on.entity_ids(room_id)


class LastOnFromEveryOrigin(LastOnHubCase):
    async def test_a_wall_switched_off_room_keeps_what_was_on(self):
        for eid in MAJLIS_LIGHTS:
            self.press(eid, "on")
        t = int(time.time())
        self.press("light.majlis_spot", "off", at=t)
        self.press("light.majlis_main", "off", at=t + 1)
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("majlis"), [])  # the fan is still on
        self.press("fan.majlis", "off", at=t + 2)
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.last_on("majlis"),
            ["fan.majlis", "light.majlis_main", "light.majlis_spot"],
        )
        self.assertEqual(
            await self.saved(),
            {
                "majlis": {
                    "v": 1,
                    "entity_ids": [
                        "fan.majlis",
                        "light.majlis_main",
                        "light.majlis_spot",
                    ],
                    "at": t + 2,
                }
            },
        )

    async def test_an_automation_switching_the_room_off(self):
        await self.call("light", "turn_on", ["light.majlis_main", "light.majlis_spot"])
        run = Context(parent_id=Context().id)
        self.hass.bus.async_fire("automation_triggered", {}, context=run)
        await self.call(
            "light", "turn_off", ["light.majlis_main", "light.majlis_spot"], run
        )
        self.assertEqual(
            self.last_on("majlis"), ["light.majlis_spot", "light.majlis_main"]
        )

    async def test_the_hubs_own_room_off(self):
        self.press("light.kitchen", "on")
        self.press("fan.kitchen_hood", "on")
        app = self.recorder.command_context(activity.SOURCE_APP, "mem-a")
        await self.call("light", "turn_off", ["light.kitchen"], app)
        await self.call("fan", "turn_off", ["fan.kitchen_hood"], app)
        self.assertEqual(self.last_on("kitchen"), ["fan.kitchen_hood", "light.kitchen"])
        # No member in it.
        self.assertNotIn("mem-a", str(await self.saved()))

    async def test_a_home_assistant_user_switching_it_off(self):
        self.press("light.bedroom", "on")
        await self.call(
            "light", "turn_off", ["light.bedroom"], Context(user_id="ha-user")
        )
        self.assertEqual(self.last_on("bedroom"), ["light.bedroom"])

    async def test_offs_long_before_the_last_one_are_not_in_it(self):
        t = int(time.time()) - 3600
        for eid in MAJLIS_LIGHTS:
            self.press(eid, "on", at=t)
        self.press("light.majlis_main", "off", at=t + 60)
        self.press("light.majlis_spot", "off", at=t + 1800)
        self.press("fan.majlis", "off", at=t + 1800 + 120)
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("majlis"), ["fan.majlis", "light.majlis_spot"])


class WhatNeverJoinsASet(LastOnHubCase):
    async def test_only_lights_and_fans_light_a_room(self):
        # Relays, outlets, ACs, covers, TVs, heaters: never in a set and
        # never keep a room lit.
        self.press("light.majlis_main", "on")
        for eid, state in (
            ("switch.majlis_relay", "on"),
            ("switch.majlis_outlet", "on"),
            ("climate.majlis_ac", "cool"),
            ("cover.majlis_curtain", "open"),
            ("media_player.majlis_tv", "playing"),
        ):
            self.press(eid, state)
        self.press("light.majlis_main", "off")
        for eid, state in (
            ("switch.majlis_relay", "off"),
            ("switch.majlis_outlet", "off"),
            ("climate.majlis_ac", "off"),
            ("cover.majlis_curtain", "closed"),
            ("media_player.majlis_tv", "off"),
        ):
            self.press(eid, state)
        self.press("switch.kitchen_heater", "on")
        self.press("water_heater.kitchen_tank", "eco")
        self.press("switch.kitchen_heater", "off")
        self.press("water_heater.kitchen_tank", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("majlis"), ["light.majlis_main"])
        self.assertEqual(self.last_on("kitchen"), [])

    async def test_unimported_excluded_and_roomless_lights_are_not_followed(self):
        for eid in (
            UNIMPORTED[0],
            "light.majlis_lamp",  # casasmart_room_activity_exclude
            "light.hall",
        ):
            self.press(eid, "on")
            self.press(eid, "off")
        # An excluded lamp left on doesn't keep the room lit either.
        self.press("light.majlis_lamp", "on")
        self.press("light.majlis_main", "on")
        self.press("light.majlis_main", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("kitchen"), [])
        self.assertEqual(self.last_on("majlis"), ["light.majlis_main"])
        self.assertEqual(set(await self.saved()), {"majlis"})

    async def test_a_light_going_unavailable_is_not_an_off(self):
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "off")
        await self.hass.async_block_till_done()
        before = (await self.saved())["bedroom"]
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "unavailable")
        self.press("light.bedroom", "off")
        await self.hass.async_block_till_done()
        self.assertEqual((await self.saved())["bedroom"], before)

    async def test_a_removed_light_is_not_an_off(self):
        self.press("light.bedroom", "on")
        self.hass.states.async_remove("light.bedroom")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("bedroom"), [])


class LastOnAcrossRestarts(LastOnHubCase):
    already_running = False
    settle_seconds = 0.3

    async def start_home_assistant(self):
        """Home Assistant starts: restores, reports, started (still settling)."""
        self.hass.set_state(CoreState.starting)
        # The flood: unavailable, restored states, devices catching up.
        self.press("light.majlis_main", "unavailable")
        self.press("light.majlis_main", "on")
        self.press("light.majlis_spot", "unavailable")
        self.hass.set_state(CoreState.running)
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done()
        self.press("light.majlis_main", "off")
        self.press("light.majlis_spot", "off")
        self.press("fan.majlis", "on")
        self.press("fan.majlis", "off")
        await self.hass.async_block_till_done()

    async def settle(self):
        await asyncio.sleep(self.settle_seconds + 0.1)  # past the settle
        await self.hass.async_block_till_done()

    async def test_the_startup_flood_never_replaces_a_saved_set(self):
        saved = {"v": 1, "entity_ids": ["light.majlis_spot"], "at": 1_700_000_000}
        await self.recorder.async_stop()
        self.storage.table("now_room_last_on")["majlis"] = saved
        self.recorder = self.new_recorder()
        await self.recorder.async_start()
        self.assertEqual(self.last_on("majlis"), ["light.majlis_spot"])

        await self.start_home_assistant()
        self.assertEqual(self.last_on("majlis"), ["light.majlis_spot"])
        self.assertEqual((await self.saved())["majlis"], saved)

        # Settled: the lights are followed from what is on now.
        self.press("light.majlis_main", "on")
        await self.settle()
        self.press("fan.majlis", "on")
        self.press("fan.majlis", "off")
        self.press("light.majlis_main", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("majlis"), ["light.majlis_main", "fan.majlis"])

    async def test_a_light_on_through_the_restart_is_followed_once_settled(self):
        await self.start_home_assistant()
        self.press("light.bedroom", "on")  # during the settle: not followed
        await self.settle()
        # ...but it is on now, so its off is the room going dark.
        self.press("light.bedroom", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("bedroom"), ["light.bedroom"])

    async def test_a_reload_follows_at_once(self):
        await self.recorder.async_stop()
        self.hass.set_state(CoreState.running)
        self.press("light.kitchen", "on")
        await self.hass.async_block_till_done()
        self.recorder = self.new_recorder()
        await self.recorder.async_start()
        self.press("light.kitchen", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("kitchen"), ["light.kitchen"])

    async def test_a_set_survives_a_restart(self):
        self.hass.set_state(CoreState.running)
        await self.recorder.async_stop()
        self.recorder = self.new_recorder()
        await self.recorder.async_start()
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "off")
        await self.hass.async_block_till_done()
        # Stopping writes what is unsaved (as on Home Assistant's stop).
        await self.recorder.async_stop()
        self.recorder = self.new_recorder()
        await self.recorder.async_start()
        self.assertEqual(self.last_on("bedroom"), ["light.bedroom"])


class LastOnAndTheRegistry(LastOnHubCase):
    async def registry_changed(self):
        self.hass.bus.async_fire(EVENT_REGISTRY_CHANGED, {"kind": "rooms"})
        await asyncio.sleep(1.2)  # the recorder's debounce
        await self.hass.async_block_till_done()

    async def test_a_deleted_room_takes_its_set_with_it(self):
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "off")
        self.press("light.kitchen", "on")
        self.press("light.kitchen", "off")
        self.assertEqual(set(await self.saved()), {"bedroom", "kitchen"})
        self.registry.delete_room("bedroom")
        await self.registry_changed()
        self.assertEqual(self.last_on("bedroom"), [])
        self.assertEqual(set(await self.saved()), {"kitchen"})

    async def test_a_light_moved_while_on_doesnt_darken_its_old_room(self):
        self.press("light.majlis_main", "on")
        self.registry.assign_device("light.majlis_main", room_id="bedroom")
        await self.registry_changed()
        self.press("light.majlis_main", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.last_on("majlis"), [])
        self.assertEqual(self.last_on("bedroom"), ["light.majlis_main"])

    async def test_factory_reset_clear_drops_saved_and_unsaved_sets(self):
        self.press("light.kitchen", "on")
        self.press("light.kitchen", "off")
        await self.saved()
        self.press("light.bedroom", "on")
        self.press("light.bedroom", "off")  # not written yet
        await self.hass.async_block_till_done()
        await self.recorder.async_clear()
        self.assertEqual(len(self.recorder.last_on), 0)
        self.assertEqual(await self.saved(), {})


if __name__ == "__main__":
    unittest.main()
