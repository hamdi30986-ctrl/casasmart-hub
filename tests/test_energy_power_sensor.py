"""Golden pin of the power sensor Energy Saving's wizard shows for each plug.

async_energy_discovery over the real registry and inventory builder, with
Home Assistant's entity registry faked: each plug's power_sensor (the first
sibling sensor on its HA device, by entity id, with device class power or
"power" in its id) is compared as JSON text. Real-HA environment only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from casasmart.energy_api import async_energy_discovery
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.helpers import entity_registry as er

    IMPORT_ERROR = H.IMPORT_ERROR
except Exception as err:
    IMPORT_ERROR = err


class _Entries:
    def __init__(self, entries):
        self._entries = entries

    def get_entries_for_device_id(self, device_id, include_disabled_entities=False):
        return [e for e in self._entries if e.device_id == device_id]


class FakeEntityRegistry:
    """The two lookups the hub makes on HA's entity registry."""

    def __init__(self, entries):
        self._by_id = {e.entity_id: e for e in entries}
        self.entities = _Entries(entries)

    def async_get(self, entity_id):
        return self._by_id.get(entity_id)


def entry(entity_id, device_id, original_device_class=None):
    return NS(
        entity_id=entity_id,
        device_id=device_id,
        original_device_class=original_device_class,
        entity_category=None,
        hidden_by=None,
        area_id=None,
    )


# (entity_id, state, attributes) on the hub's states.
STATES = [
    ("switch.plug_a", "on", {"device_class": "outlet", "friendly_name": "Plug A"}),
    ("sensor.plug_a_current", "0.1", {"device_class": "current"}),
    (
        "sensor.plug_a_energy",
        "3.2",
        {"device_class": "energy", "unit_of_measurement": "kWh"},
    ),
    (
        "sensor.plug_a_power",
        "12.5",
        {"device_class": "power", "unit_of_measurement": "W"},
    ),
    ("switch.plug_b", "off", {}),
    ("sensor.plug_b_apparent_power", "30", {"unit_of_measurement": "VA"}),
    (
        "sensor.plug_b_watts",
        "28",
        {"device_class": "power", "unit_of_measurement": "W"},
    ),
    ("switch.plug_c", "on", {"device_class": "plug"}),
    ("binary_sensor.plug_c_power", "on", {}),
    ("switch.plug_d", "on", {"device_class": "outlet"}),
    ("switch.plug_e", "unavailable", {"device_class": "outlet"}),
    ("switch.heater_f", "off", {}),
    ("switch.plug_g", "on", {"device_class": "outlet"}),
    (
        "sensor.plug_g_temp",
        "24",
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    ),
    ("switch.lamp_h", "on", {}),
]


def entries():
    """HA's entity registry entries (built lazily: SensorDeviceClass needs HA)."""
    return [
        entry("switch.plug_a", "dev-a"),
        entry("sensor.plug_a_current", "dev-a"),
        entry("sensor.plug_a_energy", "dev-a"),
        entry("sensor.plug_a_power", "dev-a"),
        entry("switch.plug_b", "dev-b"),
        entry("sensor.plug_b_apparent_power", "dev-b", "apparent_power"),
        entry("sensor.plug_b_watts", "dev-b", "power"),
        entry("switch.plug_c", "dev-c"),
        entry("binary_sensor.plug_c_power", "dev-c"),
        # Registered but without a state (disabled): its class comes from the
        # registry, as an enum.
        entry("sensor.plug_c_load", "dev-c", SensorDeviceClass.POWER),
        entry("switch.plug_e", None),
        entry("switch.heater_f", "dev-f"),
        entry("switch.plug_g", "dev-g"),
        entry("sensor.plug_g_temp", "dev-g"),
    ]


def candidate(entity_id, state, power_sensor=..., name=None):
    value = {
        "entity_id": entity_id,
        "name": name or entity_id,
        "state": state,
        "available": state not in {"unavailable", "unknown"},
    }
    if power_sensor is not ...:
        value["power_sensor"] = power_sensor
    return value


@unittest.skipIf(
    IMPORT_ERROR is not None, f"Home Assistant unavailable: {IMPORT_ERROR}"
)
class EnergyPowerSensorGoldenTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hass, self.runtime = H.make_hub(self.tmp.name)
        storage = self.runtime.storage
        self.addCleanup(storage.close)
        self.hass.data[er.DATA_REGISTRY] = FakeEntityRegistry(entries())
        storage.table("registry_rooms")["room-p"] = {
            "name": "Plant room",
            "floor_id": None,
            "icon": None,
            "sort_order": 0,
        }
        for eid, state, attributes in STATES:
            self.hass.states.add(eid, state=state, attributes=dict(attributes))
            storage.table("registry_devices")[eid] = {
                "room_id": "room-p",
                "display_name": None,
                "sort_order": 0,
            }
        storage.table("registry_user_devices")["dev-b"] = {
            "entity_ids": ["switch.plug_b"],
            "gangs": {"switch.plug_b": {"type": "outlet"}},
            "room_id": "room-p",
        }
        storage.table("registry_user_devices")["dev-f"] = {
            "entity_ids": ["switch.heater_f"],
            "gangs": {"switch.heater_f": {"type": "heater"}},
            "room_id": "room-p",
        }
        self.runtime.registry.warm_up()

    async def test_each_plug_reports_the_power_sensor_on_its_device(self):
        discovery = await async_energy_discovery(self.hass, self.runtime)
        (room,) = discovery["rooms"]
        self.assertEqual(
            json.dumps({"plugs": room["plugs"], "heaters": room["heaters"]}),
            json.dumps(
                {
                    "plugs": [
                        candidate("switch.heater_f", "off", None),
                        candidate(
                            "switch.plug_a",
                            "on",
                            {
                                "entity_id": "sensor.plug_a_power",
                                "state": "12.5",
                                "unit": "W",
                            },
                            name="Plug A",
                        ),
                        # "power" in the entity id wins before the real power
                        # sensor, because it sorts first.
                        candidate(
                            "switch.plug_b",
                            "off",
                            {
                                "entity_id": "sensor.plug_b_apparent_power",
                                "state": "30",
                                "unit": "VA",
                            },
                        ),
                        candidate(
                            "switch.plug_c",
                            "on",
                            {
                                "entity_id": "sensor.plug_c_load",
                                "state": "unavailable",
                                "unit": None,
                            },
                        ),
                        candidate("switch.plug_d", "on", None),
                        candidate("switch.plug_e", "unavailable", None),
                        candidate("switch.plug_g", "on", None),
                    ],
                    "heaters": [
                        candidate("switch.heater_f", "off"),
                        candidate("switch.plug_b", "off"),
                    ],
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
