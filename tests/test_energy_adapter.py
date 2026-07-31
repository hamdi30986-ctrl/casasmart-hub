"""P2 tests for the Home Assistant Energy Saving adapter.

The real EnergyEngine runs over temporary SQLite storage.  Only HA's event
bus, state machine, service registry, and timers are faked, which keeps the
tests focused on the adapter boundary without weakening the durable contract.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_CC = _ROOT / "custom_components"
_PKG = _CC / "casasmart"
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_CC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

import casasmart.energy_adapter as adapter_module  # noqa: E402
from casasmart.energy import (  # noqa: E402
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_SMART,
    EnergyEngine,
    default_level_config,
)
from casasmart.energy_adapter import (  # noqa: E402
    BOOST_FAILSAFE_SECONDS,
    EMPTY_GRACE_SECONDS,
    EnergyAdapter,
    EnergyInventoryBuilder,
)
from casasmart.registry import UNSET  # noqa: E402
from homeassistant.core import Event, State  # noqa: E402
from storage import HubStorage  # noqa: E402


class _Clock:
    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _State(State):
    def __init__(
        self,
        entity_id: str,
        state: str,
        attributes: dict | None = None,
        *,
        last_changed: datetime | None = None,
    ) -> None:
        super().__init__(entity_id, state, attributes)
        self.last_changed = last_changed


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = {state.entity_id: state for state in states}

    def async_all(self):
        return list(self._states.values())

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, state):
        self._states[state.entity_id] = state


class _Bus:
    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.fired: list[tuple[str, dict | None]] = []

    def async_listen(self, event_type, callback):
        self.listeners.setdefault(event_type, []).append(callback)

        def _unsubscribe():
            self.listeners[event_type].remove(callback)

        return _unsubscribe

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))

    def emit(self, event_type, data):
        for callback in list(self.listeners.get(event_type, [])):
            callback(Event(data))


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bool]] = []
        self.fail_entities: set[str] = set()

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data), blocking))
        if data["entity_id"] in self.fail_entities:
            raise RuntimeError("simulated service failure")


class _Hass:
    def __init__(self, states: list[_State]) -> None:
        self.states = _States(states)
        self.bus = _Bus()
        self.services = _Services()
        self.pending: list = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        self.pending.append(coro)
        return coro

    async def drain(self):
        while self.pending:
            await self.pending.pop(0)


class _Timers:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, hass, delay, action):
        record = {
            "delay": delay,
            "action": action,
            "cancelled": False,
        }
        self.calls.append(record)

        def _cancel():
            record["cancelled"] = True

        return _cancel

    def latest(self, delay):
        return next(
            record
            for record in reversed(self.calls)
            if round(record["delay"]) == round(delay)
        )

    def fire(self, record):
        record["action"](None)


class _Registry:
    def __init__(self, rooms: dict[str, str | None], groups=None) -> None:
        self.rooms = dict(rooms)
        self.groups = list(groups or [])

    def room_of(self, entity_id):
        return self.rooms.get(entity_id, UNSET)

    def list_user_devices(self):
        return list(self.groups)


def _group(group_id: str, room: str, *entities: str) -> dict:
    return {
        "ha_device_id": group_id,
        "room_id": room,
        "control_entity_ids": list(entities),
        "gangs": {
            entity_id: {"type": "switch", "presentation": "grouped"}
            for entity_id in entities
        },
    }


def _sun(
    now: datetime,
    *,
    day: bool,
    since_sunrise_hours: float = 2,
    until_sunset_hours: float = 4,
) -> _State:
    if day:
        changed = now - timedelta(hours=since_sunrise_hours)
        next_rising = now + timedelta(hours=18)
        next_setting = now + timedelta(hours=until_sunset_hours)
        state = "above_horizon"
    else:
        changed = now - timedelta(hours=2)
        next_rising = now + timedelta(hours=6)
        next_setting = now + timedelta(hours=18)
        state = "below_horizon"
    return _State(
        "sun.sun",
        state,
        {
            "next_rising": next_rising.isoformat(),
            "next_setting": next_setting.isoformat(),
        },
        last_changed=changed,
    )


class EnergyAdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = HubStorage(Path(self.tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.engine = EnergyEngine(
            self.storage.table("energy_config"),
            self.storage.table("energy_state"),
            self.storage.energy_events(),
        )
        self.engine.warm_up()
        self.now = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
        self.wall = _Clock(self.now.timestamp())
        self.monotonic = _Clock(1000)
        self.timers = _Timers()
        self.timer_patch = mock.patch.object(
            adapter_module, "async_call_later", self.timers
        )
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.hass: _Hass | None = None

    def tearDown(self):
        if self.hass is not None:
            for coro in self.hass.pending:
                coro.close()
            self.hass.pending.clear()

    def make_adapter(self, states, rooms, groups=None):
        self.hass = _Hass(states)
        registry = _Registry(rooms, groups)
        adapter = EnergyAdapter(
            self.hass,
            self.engine,
            registry,
            area_resolver=lambda _hass, _entity_id: None,
            wall_clock=self.wall,
            monotonic_clock=self.monotonic,
        )
        return adapter, registry

    def activate(self, level, **patch):
        config = default_level_config(level)
        config.update(patch)
        config["setup_complete"] = True
        self.engine.replace_config(level, config)
        self.engine.activate(level)

    def calls(self, entity_id=None, service=None):
        calls = self.hass.services.calls
        if entity_id is not None:
            calls = [call for call in calls if call[2]["entity_id"] == entity_id]
        if service is not None:
            calls = [call for call in calls if call[1] == service]
        return calls

    def assert_command(self, entity_id, service, **data):
        matches = self.calls(entity_id, service)
        self.assertTrue(matches, f"missing {service} for {entity_id}: {self.calls()}")
        for key, value in data.items():
            self.assertEqual(matches[-1][2].get(key), value)

    def emit_change(self, adapter, old, new):
        self.hass.states.set(new)
        self.hass.bus.emit(
            "state_changed",
            {
                "entity_id": new.entity_id,
                "old_state": old,
                "new_state": new,
            },
        )

    async def drain(self):
        await self.hass.drain()

    # -- inventory ---------------------------------------------------------

    async def test_inventory_classifies_rooms_and_excludes_gang_lights(self):
        states = [
            _State("light.real", "on", {"brightness": 100}),
            _State("light.wall_1", "on"),
            _State("light.wall_2", "on"),
            _State("sensor.temp", "25", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "off", {"device_class": "occupancy"}),
        ]
        rooms = {state.entity_id: "living" for state in states}
        adapter, registry = self.make_adapter(
            states,
            rooms,
            [_group("wall", "living", "light.wall_1", "light.wall_2")],
        )
        inventory = await EnergyInventoryBuilder(
            self.hass,
            registry,
            area_resolver=lambda _hass, _entity_id: None,
        ).async_build()
        room = inventory.rooms["living"]
        self.assertEqual([item.entity_id for item in room.lights], ["light.real"])
        self.assertEqual(len(room.temperature_sensors), 1)
        self.assertEqual(len(room.presence_sensors), 1)
        self.assertTrue(room.automatic)
        self.assertEqual(
            inventory.gangs[0].entity_ids,
            ("light.wall_1", "light.wall_2"),
        )

    async def test_plain_grouped_device_record_does_not_hide_real_lights(self):
        states = [
            _State("light.bulb_one", "on", {"brightness": 100}),
            _State("light.bulb_two", "on", {"brightness": 100}),
        ]
        rooms = {state.entity_id: "living" for state in states}
        plain_device = {
            "ha_device_id": "bulb-device",
            "room_id": "living",
            "control_entity_ids": ["light.bulb_one", "light.bulb_two"],
            "gangs": {},
            "gang_types": {},
            "device_type": "light",
        }
        _adapter, registry = self.make_adapter(
            states, rooms, [plain_device]
        )
        inventory = await EnergyInventoryBuilder(
            self.hass,
            registry,
            area_resolver=lambda _hass, _entity_id: None,
        ).async_build()
        self.assertEqual(
            [entity.entity_id for entity in inventory.rooms["living"].lights],
            ["light.bulb_one", "light.bulb_two"],
        )
        self.assertEqual(inventory.gangs, ())

    async def test_temperature_inventory_rejects_diagnostic_siblings(self):
        states = [
            _State(
                "sensor.room_temperature",
                "25",
                {"device_class": "temperature"},
            ),
            _State(
                "sensor.relay_device_temperature",
                "42",
                {"device_class": "temperature"},
            ),
            _State("sensor.room_temp_battery", "95"),
            _State("sensor.room_temp_humidity", "40"),
        ]
        rooms = {state.entity_id: "room" for state in states}
        _adapter, registry = self.make_adapter(states, rooms)
        categories = {
            "sensor.relay_device_temperature": "diagnostic",
            "sensor.room_temp_battery": "diagnostic",
        }
        inventory = await EnergyInventoryBuilder(
            self.hass,
            registry,
            area_resolver=lambda _hass, _entity_id: None,
            category_resolver=lambda _hass, entity_id: categories.get(
                entity_id
            ),
        ).async_build()
        self.assertEqual(
            [
                entity.entity_id
                for entity in inventory.rooms["room"].temperature_sensors
            ],
            ["sensor.room_temperature"],
        )

    # -- static Low / Medium ----------------------------------------------

    async def test_low_applies_full_static_matrix_without_resurrection(self):
        states = [
            _State("switch.g1", "on"),
            _State("switch.g2", "on"),
            _State("switch.g3", "on"),
            _State("switch.two1", "on"),
            _State("switch.two2", "on"),
            _State(
                "climate.cool",
                "cool",
                {"temperature": 20, "fan_modes": ["low", "high"]},
            ),
            _State(
                "climate.heat",
                "heat",
                {"temperature": 25, "fan_modes": ["low", "high"]},
            ),
            _State("climate.off", "off", {"temperature": 18}),
            _State("light.keep", "on", {"brightness": 255}),
            _State("light.kill", "on", {"brightness": 100}),
            _State("light.off_keeper", "off", {"brightness": 0}),
            _State("switch.plug", "on"),
            _State("switch.heater_off", "on"),
            _State("switch.heater_keep", "on"),
            _State("cover.curtain", "open"),
            _sun(self.now, day=True),
        ]
        rooms = {
            state.entity_id: "living"
            for state in states
            if state.entity_id != "sun.sun"
        }
        groups = [
            _group("three", "living", "switch.g1", "switch.g2", "switch.g3"),
            _group("two", "living", "switch.two1", "switch.two2"),
        ]
        adapter, _ = self.make_adapter(states, rooms, groups)
        self.activate(
            LEVEL_LOW,
            gang_keepers={"three": ["switch.g1", "switch.g2"]},
            light_keepers={"living": ["light.keep", "light.off_keeper"]},
            plug_offs=["switch.plug"],
            heaters=[
                {"entity_id": "switch.heater_off", "turn_off": True},
                {"entity_id": "switch.heater_keep", "turn_off": False},
            ],
        )
        summary = await adapter.async_apply()
        await self.drain()

        self.assertEqual(summary["failures"], 0)
        self.assert_command("switch.g3", "turn_off")
        self.assertEqual(self.calls("switch.two1"), [])
        self.assert_command("climate.cool", "set_temperature", temperature=24.0)
        self.assert_command("climate.heat", "set_temperature", temperature=21.0)
        self.assertEqual(self.calls("climate.cool", "set_fan_mode"), [])
        self.assertEqual(self.calls("climate.off"), [])
        self.assert_command("light.kill", "turn_off")
        self.assert_command("light.keep", "turn_on", brightness_pct=80)
        self.assertEqual(self.calls("light.off_keeper"), [])
        self.assert_command("switch.plug", "turn_off")
        self.assert_command("switch.heater_off", "turn_off")
        self.assertEqual(self.calls("switch.heater_keep"), [])
        self.assertEqual(self.calls("cover.curtain"), [])

    async def test_gang_requires_all_channels_on(self):
        states = [
            _State("switch.g1", "on"),
            _State("switch.g2", "off"),
            _State("switch.g3", "on"),
        ]
        rooms = {state.entity_id: "r" for state in states}
        adapter, _ = self.make_adapter(
            states, rooms, [_group("three", "r", *(s.entity_id for s in states))]
        )
        self.activate(
            LEVEL_LOW,
            gang_keepers={"three": ["switch.g1", "switch.g2"]},
        )
        await adapter.async_apply()
        self.assertEqual(self.calls(), [])

    async def test_medium_keeper_force_fan_guard_and_day_covers(self):
        states = [
            _State("switch.g1", "on"),
            _State("switch.g2", "on"),
            _State(
                "climate.keep",
                "cool",
                {"temperature": 20, "fan_modes": ["low", "high"]},
            ),
            _State(
                "climate.kill",
                "cool",
                {"temperature": 20, "fan_modes": ["low", "high"]},
            ),
            _State("sensor.temp", "21", {"device_class": "temperature"}),
            _State("cover.curtain", "open"),
            _sun(self.now, day=True),
        ]
        rooms = {
            state.entity_id: "bed"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(
            states,
            rooms,
            [_group("two", "bed", "switch.g1", "switch.g2")],
        )
        self.activate(
            LEVEL_MEDIUM,
            gang_keepers={"two": ["switch.g1"]},
            ac_keepers={"bed": ["climate.keep"]},
        )
        await adapter.async_apply()
        await self.drain()
        self.assert_command("switch.g2", "turn_off")
        self.assert_command("climate.kill", "turn_off")
        # Room guard wins before force-24/fan-low.
        self.assert_command("climate.keep", "turn_off")
        self.assertEqual(self.calls("climate.keep", "set_temperature"), [])
        self.assert_command("cover.curtain", "close_cover")

    async def test_medium_forces_24_and_low_fan_when_room_is_not_cold(self):
        states = [
            _State(
                "climate.ac",
                "cool",
                {"temperature": 19, "fan_modes": ["Low", "High"]},
            ),
            _State("sensor.temp", "25", {"device_class": "temperature"}),
        ]
        rooms = {state.entity_id: "bed" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_MEDIUM)
        await adapter.async_apply()
        self.assert_command("climate.ac", "set_temperature", temperature=24.0)
        self.assert_command("climate.ac", "set_fan_mode", fan_mode="Low")

    async def test_excluded_and_sensor_troubled_rooms_are_completely_skipped(self):
        states = [
            _State("light.excluded1", "on", {"brightness": 255}),
            _State("light.excluded2", "on", {"brightness": 255}),
            _State("light.bad1", "on", {"brightness": 255}),
            _State("light.bad2", "on", {"brightness": 255}),
            _State("sensor.bad_temp", "unavailable", {"device_class": "temperature"}),
        ]
        rooms = {
            "light.excluded1": "excluded",
            "light.excluded2": "excluded",
            "light.bad1": "bad",
            "light.bad2": "bad",
            "sensor.bad_temp": "bad",
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(
            LEVEL_LOW,
            excluded_rooms=["excluded"],
            light_keepers={
                "bad": ["light.bad1"],
            },
        )
        summary = await adapter.async_apply()
        await self.drain()
        self.assertEqual(self.calls(), [])
        self.assertEqual(summary["issues"][0]["code"], "sensor_unavailable")

    # -- Smart static and occupied posture --------------------------------

    async def test_smart_sensorless_uses_static_picks_only(self):
        states = [
            _State("climate.keep", "off", {"temperature": 18}),
            _State("climate.kill", "cool", {"temperature": 18}),
            _State("light.keep", "on", {"brightness": 255}),
            _State("light.kill", "on", {"brightness": 255}),
            _State("cover.curtain", "open"),
            _sun(self.now, day=True),
        ]
        rooms = {
            state.entity_id: "office"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(
            LEVEL_SMART,
            ac_keepers={"office": ["climate.keep"]},
            light_keepers={"office": ["light.keep"]},
        )
        await adapter.async_apply()
        self.assertEqual(self.calls("climate.keep"), [])
        self.assert_command("climate.kill", "turn_off")
        self.assert_command("light.kill", "turn_off")
        self.assert_command("light.keep", "turn_on", brightness_pct=40)
        self.assert_command("cover.curtain", "close_cover")

    async def test_smart_hot_occupied_room_boosts_and_welcomes_at_night(self):
        states = [
            _State("climate.ac", "off", {"fan_modes": ["low", "max"]}),
            _State("light.one", "off", {"brightness": 0}),
            _State("light.two", "off", {"brightness": 0}),
            _State("sensor.temp", "30", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "on", {"device_class": "occupancy"}),
            _State("cover.curtain", "open"),
            _sun(self.now, day=False),
        ]
        rooms = {
            state.entity_id: "suite"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(
            LEVEL_SMART,
            light_keepers={"suite": ["light.two"]},
        )
        await adapter.async_apply()
        await self.drain()
        self.assert_command(
            "climate.ac", "set_temperature", temperature=16.0, hvac_mode="cool"
        )
        self.assert_command("climate.ac", "set_fan_mode", fan_mode="max")
        self.assert_command("light.two", "turn_on", brightness_pct=60)
        self.assertEqual(self.calls("light.one"), [])
        self.assert_command("cover.curtain", "close_cover")
        self.assertFalse(
            self.engine.snapshot()["room_occupancy"]["suite"]["occupied"] is False
        )
        self.assertEqual(
            round(self.timers.latest(BOOST_FAILSAFE_SECONDS)["delay"]),
            BOOST_FAILSAFE_SECONDS,
        )

    async def test_smart_day_welcome_opens_cover_without_lights(self):
        states = [
            _State("sensor.temp", "25", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "on", {"device_class": "occupancy"}),
            _State("light.one", "off", {"brightness": 0}),
            _State("light.two", "off", {"brightness": 0}),
            _State("cover.curtain", "closed"),
            _sun(self.now, day=True),
        ]
        rooms = {
            state.entity_id: "suite"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        await adapter.async_apply()
        self.assertEqual(self.calls("light.one"), [])
        self.assertEqual(self.calls("light.two"), [])
        self.assert_command("cover.curtain", "open_cover")

    async def test_smart_cool_guard_and_heat_mirror(self):
        states = [
            _State("climate.cool", "cool", {"fan_modes": ["low", "max"]}),
            _State("sensor.cool_temp", "21", {"device_class": "temperature"}),
            _State("binary_sensor.cool_presence", "on", {"device_class": "occupancy"}),
            _State("climate.heat", "heat", {"fan_modes": ["low", "max"]}),
            _State("sensor.heat_temp", "17", {"device_class": "temperature"}),
            _State("binary_sensor.heat_presence", "on", {"device_class": "occupancy"}),
        ]
        rooms = {
            "climate.cool": "cool_room",
            "sensor.cool_temp": "cool_room",
            "binary_sensor.cool_presence": "cool_room",
            "climate.heat": "heat_room",
            "sensor.heat_temp": "heat_room",
            "binary_sensor.heat_presence": "heat_room",
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        await adapter.async_apply()
        await self.drain()
        self.assert_command("climate.cool", "turn_off")
        self.assert_command(
            "climate.heat", "set_temperature", temperature=30.0
        )
        self.assert_command("climate.heat", "set_fan_mode", fan_mode="max")

    async def test_boost_settles_on_temperature_edge(self):
        temp = _State("sensor.temp", "30", {"device_class": "temperature"})
        states = [
            _State("climate.ac", "cool", {"fan_modes": ["low", "max"]}),
            temp,
            _State("binary_sensor.presence", "on", {"device_class": "occupancy"}),
        ]
        rooms = {state.entity_id: "suite" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        boost_timer = self.timers.latest(BOOST_FAILSAFE_SECONDS)
        before = len(self.calls("climate.ac"))

        new_temp = _State("sensor.temp", "28", {"device_class": "temperature"})
        self.emit_change(adapter, temp, new_temp)
        await self.drain()
        after_calls = self.calls("climate.ac")[before:]
        self.assertTrue(
            any(
                call[1] == "set_temperature"
                and call[2]["temperature"] == 24.0
                for call in after_calls
            )
        )
        self.assertTrue(
            any(
                call[1] == "set_fan_mode"
                and call[2]["fan_mode"] == "low"
                for call in after_calls
            )
        )
        self.assertTrue(boost_timer["cancelled"])

    async def test_boost_failsafe_settles_only_while_room_still_occupied(self):
        states = [
            _State("climate.ac", "cool", {"fan_modes": ["low", "max"]}),
            _State("sensor.temp", "30", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "on", {"device_class": "occupancy"}),
        ]
        rooms = {state.entity_id: "suite" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        await adapter.async_apply()
        await self.drain()
        before = len(self.calls("climate.ac"))
        self.timers.fire(self.timers.latest(BOOST_FAILSAFE_SECONDS))
        await self.drain()
        calls = self.calls("climate.ac")[before:]
        self.assertTrue(any(call[2].get("temperature") == 24.0 for call in calls))

    # -- empty grace / releases -------------------------------------------

    async def test_mid_boost_exit_cancels_boost_and_turns_ac_off(self):
        presence = _State(
            "binary_sensor.presence",
            "on",
            {"device_class": "occupancy"},
        )
        states = [
            _State("climate.ac", "cool", {"fan_modes": ["low", "max"]}),
            _State("sensor.temp", "30", {"device_class": "temperature"}),
            presence,
        ]
        rooms = {state.entity_id: "suite" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        boost_timer = self.timers.latest(BOOST_FAILSAFE_SECONDS)

        empty = _State(
            "binary_sensor.presence",
            "off",
            {"device_class": "occupancy"},
        )
        self.emit_change(adapter, presence, empty)
        await self.drain()
        self.timers.fire(self.timers.latest(EMPTY_GRACE_SECONDS))
        await self.drain()
        self.assertTrue(boost_timer["cancelled"])
        self.assert_command("climate.ac", "turn_off")

    async def test_empty_grace_cancels_when_presence_returns(self):
        presence = _State("binary_sensor.presence", "on", {"device_class": "occupancy"})
        states = [
            _State("sensor.temp", "25", {"device_class": "temperature"}),
            presence,
            _State("light.one", "on", {"brightness": 100}),
        ]
        rooms = {state.entity_id: "suite" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()

        off = _State("binary_sensor.presence", "off", {"device_class": "occupancy"})
        self.emit_change(adapter, presence, off)
        await self.drain()
        empty_timer = self.timers.latest(EMPTY_GRACE_SECONDS)
        self.assertFalse(empty_timer["cancelled"])
        self.assertEqual(self.calls("light.one", "turn_off"), [])

        on_again = _State("binary_sensor.presence", "on", {"device_class": "occupancy"})
        self.emit_change(adapter, off, on_again)
        await self.drain()
        self.assertTrue(empty_timer["cancelled"])

    async def test_empty_expiry_self_heals_releases_and_kills_room(self):
        states = [
            _State("climate.ac", "cool", {"fan_modes": ["low", "max"]}),
            _State("sensor.temp", "25", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "off", {"device_class": "occupancy"}),
            _State("light.one", "on", {"brightness": 100}),
            _State("switch.plug", "on"),
            _State("cover.curtain", "open"),
            _sun(self.now, day=True),
        ]
        rooms = {
            state.entity_id: "suite"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART, plug_offs=["switch.plug"])
        self.engine.mark_released("light.one", room_id="suite")
        await adapter.async_apply()
        await self.drain()
        timer = self.timers.latest(EMPTY_GRACE_SECONDS)
        self.timers.fire(timer)
        await self.drain()
        self.assert_command("climate.ac", "turn_off")
        self.assert_command("light.one", "turn_off")
        self.assert_command("switch.plug", "turn_off")
        self.assert_command("cover.curtain", "close_cover")
        self.assertFalse(self.engine.is_released("light.one"))
        self.assertFalse(
            self.engine.snapshot()["room_occupancy"]["suite"]["occupied"]
        )

    async def test_unavailable_smart_sensor_skips_room_and_warns(self):
        states = [
            _State("climate.ac", "cool", {"fan_modes": ["low", "max"]}),
            _State("sensor.temp", "unavailable", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "on", {"device_class": "occupancy"}),
            _State("light.one", "on", {"brightness": 255}),
            _State("light.two", "on", {"brightness": 255}),
        ]
        rooms = {state.entity_id: "suite" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(
            LEVEL_SMART,
            light_keepers={"suite": ["light.one"]},
        )
        summary = await adapter.async_apply()
        await self.drain()
        self.assertEqual(self.calls(), [])
        self.assertEqual(summary["issues"][0]["code"], "sensor_unavailable")
        occupancy = self.engine.snapshot()["room_occupancy"]["suite"]
        self.assertIsNone(occupancy["occupied"])
        self.assertFalse(occupancy["sensors_available"])

    async def test_recovered_temperature_sensor_resumes_smart_room(self):
        bad_temp = _State(
            "sensor.temp",
            "unavailable",
            {"device_class": "temperature"},
        )
        states = [
            _State("climate.ac", "off", {"fan_modes": ["low", "max"]}),
            bad_temp,
            _State(
                "binary_sensor.presence",
                "on",
                {"device_class": "occupancy"},
            ),
        ]
        rooms = {state.entity_id: "suite" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        self.assertTrue(adapter.issues())

        recovered = _State(
            "sensor.temp",
            "25",
            {"device_class": "temperature"},
        )
        self.emit_change(adapter, bad_temp, recovered)
        await self.drain()
        occupancy = self.engine.snapshot()["room_occupancy"]["suite"]
        self.assertTrue(occupancy["occupied"])
        self.assertTrue(occupancy["sensors_available"])
        self.assertEqual(adapter.issues(), [])
        self.assert_command(
            "climate.ac",
            "set_temperature",
            temperature=24.0,
            hvac_mode="cool",
        )

    async def test_own_command_is_not_release_but_later_manual_edge_is(self):
        light = _State("light.one", "on", {"brightness": 255})
        states = [
            light,
            _State("light.two", "on", {"brightness": 255}),
        ]
        rooms = {state.entity_id: "room" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(
            LEVEL_LOW,
            light_keepers={"room": ["light.one"]},
        )
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        own_new = _State("light.one", "on", {"brightness": 204})
        self.emit_change(adapter, light, own_new)
        await self.drain()
        self.assertFalse(self.engine.is_released("light.one"))

        self.monotonic.advance(16)
        manual = _State("light.one", "off", {"brightness": 0})
        self.emit_change(adapter, own_new, manual)
        await self.drain()
        self.assertTrue(self.engine.is_released("light.one"))

    async def test_released_device_is_skipped_by_medium_temperature_trigger(self):
        climate = _State(
            "climate.ac",
            "cool",
            {"temperature": 24, "fan_modes": ["low"]},
        )
        temp = _State("sensor.temp", "25", {"device_class": "temperature"})
        states = [climate, temp]
        rooms = {state.entity_id: "room" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_MEDIUM)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        self.engine.mark_released("climate.ac", room_id="room")
        before = len(self.calls("climate.ac"))
        cold = _State("sensor.temp", "20", {"device_class": "temperature"})
        self.emit_change(adapter, temp, cold)
        await self.drain()
        self.assertEqual(len(self.calls("climate.ac")), before)

    async def test_static_state_edge_releases_without_reapplying_rule(self):
        first = _State("light.one", "on", {"brightness": 255})
        second = _State("light.two", "on", {"brightness": 255})
        states = [first, second]
        rooms = {state.entity_id: "room" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(
            LEVEL_LOW,
            light_keepers={"room": ["light.one"]},
        )
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        initial_off_calls = len(self.calls("light.two", "turn_off"))

        self.monotonic.advance(16)
        manual_on = _State("light.two", "on", {"brightness": 200})
        self.emit_change(adapter, second, manual_on)
        await self.drain()
        self.assertTrue(self.engine.is_released("light.two"))
        self.assertEqual(
            len(self.calls("light.two", "turn_off")),
            initial_off_calls,
        )

    async def test_one_gang_switch_is_never_managed_or_released(self):
        switch = _State("switch.single", "off")
        adapter, _ = self.make_adapter(
            [switch],
            {"switch.single": "room"},
            [_group("single", "room", "switch.single")],
        )
        self.activate(LEVEL_LOW)
        adapter.async_start()
        await adapter.async_apply()
        manual = _State("switch.single", "on")
        self.emit_change(adapter, switch, manual)
        await self.drain()
        self.assertFalse(self.engine.is_released("switch.single"))

    async def test_medium_heat_guard_runs_on_temperature_edge(self):
        climate = _State(
            "climate.heat",
            "heat",
            {"temperature": 21, "fan_modes": ["low"]},
        )
        temp = _State("sensor.temp", "23", {"device_class": "temperature"})
        adapter, _ = self.make_adapter(
            [climate, temp],
            {"climate.heat": "room", "sensor.temp": "room"},
        )
        self.activate(LEVEL_MEDIUM)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        hot = _State("sensor.temp", "25", {"device_class": "temperature"})
        self.emit_change(adapter, temp, hot)
        await self.drain()
        self.assert_command("climate.heat", "turn_off")

    # -- failure isolation / sun / lifecycle ------------------------------

    async def test_command_failure_is_isolated_and_reported(self):
        states = [
            _State("switch.bad", "on"),
            _State("switch.good", "on"),
        ]
        rooms = {state.entity_id: "room" for state in states}
        adapter, _ = self.make_adapter(states, rooms)
        self.hass.services.fail_entities.add("switch.bad")
        self.activate(
            LEVEL_LOW,
            plug_offs=["switch.bad", "switch.good"],
        )
        summary = await adapter.async_apply()
        await self.drain()
        self.assertEqual(summary["failures"], 1)
        self.assert_command("switch.good", "turn_off")
        self.assertTrue(
            any(
                issue["code"] == "command_failed"
                for issue in summary["issues"]
            )
        )

    async def test_medium_schedules_window_start_and_does_nothing_at_window_end(self):
        # Day but only 30 minutes after sunrise: not yet in the heat window.
        sun = _sun(
            self.now,
            day=True,
            since_sunrise_hours=0.5,
            until_sunset_hours=5,
        )
        states = [_State("cover.curtain", "open"), sun]
        rooms = {"cover.curtain": "room"}
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_MEDIUM)
        await adapter.async_apply()
        self.assertEqual(self.calls("cover.curtain"), [])
        start_timer = self.timers.latest(30 * 60)

        # Move wall time to the start and fire the exact timer.
        self.wall.advance(30 * 60)
        self.timers.fire(start_timer)
        await self.drain()
        self.assert_command("cover.curtain", "close_cover")
        # There is no timer at one hour before sunset; only next day's start.
        self.assertFalse(
            any(
                round(call["delay"]) == 4 * 60 * 60
                for call in self.timers.calls
                if call is not start_timer
            )
        )

    async def test_smart_occupied_cover_follows_sunset_edge(self):
        sun = _sun(self.now, day=True)
        cover = _State("cover.curtain", "closed")
        states = [
            _State("sensor.temp", "25", {"device_class": "temperature"}),
            _State(
                "binary_sensor.presence",
                "on",
                {"device_class": "occupancy"},
            ),
            cover,
            sun,
        ]
        rooms = {
            state.entity_id: "suite"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        before = len(self.calls("cover.curtain", "close_cover"))

        self.hass.states.set(_State("cover.curtain", "open"))
        night = _sun(self.now, day=False)
        self.emit_change(adapter, sun, night)
        await self.drain()
        self.assertEqual(
            len(self.calls("cover.curtain", "close_cover")),
            before + 1,
        )

    async def test_stop_is_idempotent_and_cancels_every_timer(self):
        states = [
            _State("climate.ac", "cool", {"fan_modes": ["low", "max"]}),
            _State("sensor.temp", "30", {"device_class": "temperature"}),
            _State("binary_sensor.presence", "on", {"device_class": "occupancy"}),
            _sun(self.now, day=False),
        ]
        rooms = {
            state.entity_id: "room"
            for state in states
            if state.entity_id != "sun.sun"
        }
        adapter, _ = self.make_adapter(states, rooms)
        self.activate(LEVEL_SMART)
        adapter.async_start()
        await adapter.async_apply()
        await self.drain()
        adapter.async_stop()
        adapter.async_stop()
        self.assertEqual(self.hass.bus.listeners["state_changed"], [])
        self.assertTrue(all(timer["cancelled"] for timer in self.timers.calls))


if __name__ == "__main__":
    unittest.main()
