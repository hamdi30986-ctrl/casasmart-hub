"""Unit tests for the exposure / assignability filter.

Phase 1 (device-organize persistence): the registry WRITE surface is wider
than the read/feed surface. ``is_served`` gates the feed — entities the
integration hid (``hidden_by``) or uncurated diagnostics are invisible.
``is_assignable`` gates registry WRITES (assign a room, rename, reorder,
clear), which must still work for a device the integration hid AFTER it was
imported. Gating writes on ``is_served`` 404'd those legitimate edits — the
device was assignable at import, then the integration hid it, and the hub
refused to let the app move or clear it.

``filtering`` is package-relative and pulls in Home Assistant, so it's
imported as a submodule of the ``casasmart`` package, and the suite skips
where HA isn't installed (the HA-free local unittest env). It runs in CI and
the hub container:

    python3 -m unittest tests.test_filtering -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

# filtering uses package-relative imports, so put the PARENT of the package on
# the path and import it as casasmart.filtering. Repo layout in CI; fall back
# to the deployed tree inside the homeassistant-dev container.
_PARENT = Path(__file__).resolve().parent.parent / "custom_components"
if not (_PARENT / "casasmart").exists():
    _PARENT = Path("/config/custom_components")
sys.path.insert(0, str(_PARENT))

try:
    from casasmart import filtering
except Exception as err:  # noqa: BLE001 — Home Assistant not installed here
    filtering = None
    _IMPORT_ERROR: Exception | None = err
else:
    _IMPORT_ERROR = None


def _hass(states=None):
    """A duck-typed hass whose ``states.get`` reads a dict."""
    hass = mock.Mock()
    hass.states.get.side_effect = (states or {}).get
    return hass


def _entity_registry(existing):
    """Fake entity registry — ``existing`` is the set of entity_ids present."""
    registry = mock.Mock()
    registry.async_get.side_effect = (
        lambda entity_id: object() if entity_id in existing else None
    )
    return registry


@unittest.skipIf(
    filtering is None, f"casasmart.filtering unimportable: {_IMPORT_ERROR}"
)
class IsAssignableTests(unittest.TestCase):
    def test_hidden_entity_with_registry_entry_is_assignable(self):
        # A real gang switch the Z2M integration set hidden_by='integration':
        # is_served would be False, but it must stay re-organizable.
        with mock.patch.object(filtering, "er") as er_mod:
            er_mod.async_get.return_value = _entity_registry({"switch.bedroom1"})
            self.assertTrue(filtering.is_assignable(_hass(), "switch.bedroom1"))

    def test_yaml_entity_no_registry_entry_but_live_state_is_assignable(self):
        # template / MQTT-yaml entity: no registry entry, but a live state.
        with mock.patch.object(filtering, "er") as er_mod:
            er_mod.async_get.return_value = _entity_registry(set())
            hass = _hass({"switch.template": object()})
            self.assertTrue(filtering.is_assignable(hass, "switch.template"))

    def test_ghost_entity_is_not_assignable(self):
        # No registry entry AND no state -> a typo / deleted id is rejected,
        # so the gate still 404s genuinely-unreal entities.
        with mock.patch.object(filtering, "er") as er_mod:
            er_mod.async_get.return_value = _entity_registry(set())
            self.assertFalse(filtering.is_assignable(_hass(), "switch.ghost"))

    def test_unexposed_domain_is_not_assignable(self):
        # Outside the CasaSmart surface, even with a registry entry + state.
        with mock.patch.object(filtering, "er") as er_mod:
            er_mod.async_get.return_value = _entity_registry({"weather.home"})
            hass = _hass({"weather.home": object()})
            self.assertFalse(filtering.is_assignable(hass, "weather.home"))


@unittest.skipIf(
    filtering is None, f"casasmart.filtering unimportable: {_IMPORT_ERROR}"
)
class WeatherServiceTests(unittest.TestCase):
    @mock.patch.object(filtering, "er")
    def test_weather_service_sensor_is_excluded(self, er_mod):
        # A forecast sensor whose device also hosts a weather.* entity is a
        # service, not a home device.
        registry = mock.Mock()
        registry.async_get.return_value = mock.Mock(device_id="owm-dev")
        er_mod.async_get.return_value = registry
        er_mod.async_entries_for_device.return_value = [
            mock.Mock(entity_id="weather.openweathermap"),
            mock.Mock(entity_id="sensor.openweathermap_temperature"),
        ]
        self.assertTrue(
            filtering.is_weather_service_entity(
                _hass(), "sensor.openweathermap_temperature"
            )
        )

    @mock.patch.object(filtering, "er")
    def test_real_sensor_is_not_weather_service(self, er_mod):
        # A physical temp/humidity sensor — no weather.* sibling.
        registry = mock.Mock()
        registry.async_get.return_value = mock.Mock(device_id="aqara-dev")
        er_mod.async_get.return_value = registry
        er_mod.async_entries_for_device.return_value = [
            mock.Mock(entity_id="sensor.bedroom_temperature"),
            mock.Mock(entity_id="sensor.bedroom_humidity"),
        ]
        self.assertFalse(
            filtering.is_weather_service_entity(
                _hass(), "sensor.bedroom_temperature"
            )
        )

    def test_non_sensor_short_circuits(self):
        # Non-sensor domains never hit the registry lookup.
        self.assertFalse(
            filtering.is_weather_service_entity(_hass(), "switch.kitchen")
        )


@unittest.skipIf(
    filtering is None, f"casasmart.filtering unimportable: {_IMPORT_ERROR}"
)
class IsServedTests(unittest.TestCase):
    """is_served is the SHIPPED gate (REST + WS). Pin exposed + visible
    (hidden_by/entity_category None) so the weather-service exclusion — the
    clause that actually ships — is the variable under test."""

    @mock.patch.object(filtering, "er")
    def test_weather_service_sensor_is_not_served(self, er_mod):
        entry = mock.Mock(hidden_by=None, entity_category=None, device_id="owm")
        registry = mock.Mock()
        registry.async_get.return_value = entry
        er_mod.async_get.return_value = registry
        er_mod.async_entries_for_device.return_value = [
            mock.Mock(entity_id="weather.openweathermap"),
            mock.Mock(entity_id="sensor.openweathermap_temperature"),
        ]
        self.assertFalse(
            filtering.is_served(_hass(), "sensor.openweathermap_temperature")
        )

    @mock.patch.object(filtering, "er")
    def test_real_sensor_is_served(self, er_mod):
        entry = mock.Mock(
            hidden_by=None, entity_category=None, device_id="aqara"
        )
        registry = mock.Mock()
        registry.async_get.return_value = entry
        er_mod.async_get.return_value = registry
        er_mod.async_entries_for_device.return_value = [
            mock.Mock(entity_id="sensor.bedroom_temperature"),
            mock.Mock(entity_id="sensor.bedroom_humidity"),
        ]
        self.assertTrue(
            filtering.is_served(_hass(), "sensor.bedroom_temperature")
        )


if __name__ == "__main__":
    unittest.main()
