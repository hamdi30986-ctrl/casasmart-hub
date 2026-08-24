"""Focused registry-bound tests for outdoor weather feed exposure."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest


ROOT = Path(__file__).parents[1]


def _module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _load_filtering():
    ha = _module("homeassistant", package=True)
    core = _module("homeassistant.core")
    helpers = _module("homeassistant.helpers", package=True)
    area_registry = _module("homeassistant.helpers.area_registry")
    device_registry = _module("homeassistant.helpers.device_registry")
    entity_registry = _module("homeassistant.helpers.entity_registry")
    ha.core = core
    ha.helpers = helpers
    core.HomeAssistant = object
    core.State = object
    helpers.area_registry = area_registry
    helpers.device_registry = device_registry
    helpers.entity_registry = entity_registry

    casa = _module("casasmart", package=True)
    const = _module("casasmart.const")
    const.DOMAIN = "casasmart"
    bridge = _module("casasmart.entity_bridge")
    bridge.is_category_served = lambda category, entity_id, device_class: (
        category == "diagnostic" and device_class in {"temperature", "humidity"}
    )
    bridge.is_exposed = lambda entity_id: entity_id.startswith("sensor.")
    bridge.serialize_state = lambda *args, **kwargs: {}
    registry_module = _module("casasmart.registry")
    registry_module.UNSET = object()
    casa.const = const
    casa.entity_bridge = bridge
    casa.registry = registry_module

    spec = importlib.util.spec_from_file_location(
        "casasmart.filtering",
        ROOT / "custom_components" / "casasmart" / "filtering.py",
    )
    assert spec and spec.loader
    filtering = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = filtering
    spec.loader.exec_module(filtering)
    return filtering, entity_registry


_FILTERING, _ER = _load_filtering()


class _EntityRegistry:
    def __init__(self, entries):
        self.entries = {entry.entity_id: entry for entry in entries}

    def async_get(self, entity_id):
        return self.entries.get(entity_id)


class _States:
    def __init__(self, states):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id):
        return self._states.get(entity_id)


def _entry(entity_id, *, device_id="weather-device", hidden_by=None, config=None):
    return SimpleNamespace(
        entity_id=entity_id,
        device_id=device_id,
        hidden_by=hidden_by,
        entity_category=SimpleNamespace(value="diagnostic"),
        original_device_class=None,
        config_entry_id=config,
        area_id=None,
    )


def _state(entity_id, device_class, value="24.0"):
    return SimpleNamespace(
        entity_id=entity_id,
        state=value,
        attributes={"device_class": device_class},
    )


class OutdoorWeatherFilteringTest(unittest.TestCase):
    def setUp(self):
        self.temperature = _entry(
            "sensor.provider_temperature", hidden_by=SimpleNamespace(value="integration")
        )
        self.humidity = _entry(
            "sensor.provider_humidity", hidden_by=SimpleNamespace(value="integration")
        )
        self.weather = _entry(
            "weather.provider", config="openweather-entry", hidden_by=None
        )
        self.registry = _EntityRegistry(
            [self.temperature, self.humidity, self.weather]
        )
        _ER.async_get = lambda hass: self.registry
        _ER.async_entries_for_device = lambda registry, device_id, **kwargs: [
            entry
            for entry in registry.entries.values()
            if entry.device_id == device_id
        ]
        self.hass = SimpleNamespace(
            states=_States(
                [
                    _state(self.temperature.entity_id, "temperature"),
                    _state(self.humidity.entity_id, "humidity", "41"),
                ]
            ),
            config_entries=SimpleNamespace(
                async_get_entry=lambda entry_id: SimpleNamespace(
                    domain="openweathermap"
                )
                if entry_id == "openweather-entry"
                else None
            ),
        )

    def test_hidden_openweathermap_temperature_and_humidity_are_served(self):
        self.assertTrue(
            _FILTERING.is_served(self.hass, self.temperature.entity_id)
        )
        self.assertTrue(_FILTERING.is_served(self.hass, self.humidity.entity_id))

    def test_unrelated_hidden_sensor_stays_private(self):
        hidden = _entry(
            "sensor.private_temperature",
            device_id="private-device",
            hidden_by=SimpleNamespace(value="user"),
        )
        self.registry.entries[hidden.entity_id] = hidden
        self.hass.states._states[hidden.entity_id] = _state(
            hidden.entity_id, "temperature"
        )
        self.assertFalse(_FILTERING.is_served(self.hass, hidden.entity_id))

    def test_disabled_or_missing_measurement_fails_closed(self):
        self.hass.states._states.pop(self.temperature.entity_id)
        self.assertFalse(
            _FILTERING.is_served(self.hass, self.temperature.entity_id)
        )

    def test_non_environmental_weather_child_stays_filtered(self):
        pressure = _entry(
            "sensor.provider_pressure",
            hidden_by=SimpleNamespace(value="integration"),
        )
        self.registry.entries[pressure.entity_id] = pressure
        self.hass.states._states[pressure.entity_id] = _state(
            pressure.entity_id, "pressure"
        )
        self.assertFalse(_FILTERING.is_served(self.hass, pressure.entity_id))


if __name__ == "__main__":
    unittest.main()
