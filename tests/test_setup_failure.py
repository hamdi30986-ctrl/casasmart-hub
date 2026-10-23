"""A setup that fails once storage is open leaves nothing running.

Home Assistant never calls async_unload_entry for a failed setup, so whatever
setup started (the TLS listener, the SQLite connection, the alarm adapter's
listeners) used to stay up; after a reload the hub ran two alarm engines. The
real async_setup_entry runs here with real storage, TLS listener and alarm
adapter; HA-facing helpers that need a running Home Assistant are faked, and a
late step is made to fail. Needs a real Home Assistant.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    from homeassistant.core import CoreState
    from homeassistant.exceptions import ConfigEntryError

    integration = H.import_integration()
    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


class _Bus:
    def __init__(self) -> None:
        self.listeners: list[tuple[str, object]] = []

    def async_listen(self, event_type, handler, event_filter=None):
        item = (event_type, handler)
        self.listeners.append(item)
        return lambda: self.listeners.remove(item)

    async_listen_once = async_listen

    def async_fire(self, event_type, data=None):
        pass


class _Entry:
    def __init__(self) -> None:
        self.entry_id = "entry"
        self.options: dict = {}
        self.data: dict = {}
        self.on_unload: list = []

    def async_on_unload(self, func) -> None:
        self.on_unload.append(func)

    def run_on_unload(self) -> None:
        """What Home Assistant does after a failed setup."""
        while self.on_unload:
            self.on_unload.pop()()


class _Mdns:
    instances: list[_Mdns] = []

    def __init__(self, *args, **kwargs) -> None:
        self.stopped = False
        _Mdns.instances.append(self)

    async def async_start(self) -> None:
        pass

    async def async_refresh(self, _now=None) -> None:
        pass

    async def async_stop(self) -> None:
        self.stopped = True


class _Suggestions:
    def __init__(self, *args, **kwargs) -> None:
        self.stopped = False

    async def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


class _FailingEnergyController:
    """The injected fault: the first storage read of the Energy Saving start."""

    def __init__(self, *args, **kwargs) -> None:
        self.stopped = False

    async def async_start(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    async def async_stop(self) -> None:
        self.stopped = True


@unittest.skipIf(_SKIP, f"CasaSmart unimportable: {_SKIP}")
class FailedSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "casasmart"
        self.data_dir.mkdir()
        self.port = _free_port()
        (self.data_dir / "hub_config.json").write_text(
            json.dumps({"tls_port": self.port, "registry_imported": True})
        )

        async def _executor(func, *args):
            return func(*args)

        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(
                path=lambda name: str(Path(self._tmp.name) / name)
            ),
            async_add_executor_job=_executor,
            bus=_Bus(),
            services=H.FakeServices(),
            data={},
            is_stopping=False,
            # Still starting: the room activity recorder waits for "started".
            state=CoreState.starting,
        )
        self.entry = _Entry()
        _Mdns.instances.clear()
        for name, value in (
            ("async_clear_legacy_update_dirs", mock.AsyncMock()),
            ("SuggestionRuntime", _Suggestions),
            ("persistent_notification", mock.Mock()),
            ("notify_recovery_code", mock.Mock()),
            (
                "async_get_integration",
                mock.AsyncMock(return_value=types.SimpleNamespace(version="1.0")),
            ),
            ("async_register_views", mock.Mock()),
            ("build_views", mock.Mock(return_value=[])),
            ("async_track_time_interval", mock.Mock(return_value=lambda: None)),
            ("MdnsAdvertiser", _Mdns),
            ("EnergyAdapter", mock.Mock()),
            ("EnergyAutomationManager", mock.Mock()),
            ("EnergyController", _FailingEnergyController),
        ):
            patcher = mock.patch.object(integration, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_late_failure_stops_everything_and_closes_storage(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            await integration.async_setup_entry(self.hass, self.entry)
        self.entry.run_on_unload()

        runtime = self.entry.runtime_data
        self.assertTrue(runtime.tls is not None and _port_is_free(self.port))
        self.assertTrue(_Mdns.instances[0].stopped)
        self.assertTrue(runtime.energy_controller.stopped)
        self.assertTrue(runtime.suggestions.stopped)
        # The real room activity recorder started; its listeners must go too.
        self.assertIsNotNone(runtime.room_activity)
        # The alarm adapter's listeners are what made a reload run two alarms.
        self.assertEqual(self.hass.bus.listeners, [])
        self.assertIsNone(runtime.storage._conn)

    async def test_home_assistant_stop_writes_room_activity_before_closing(self):
        # Home Assistant doesn't unload entries when it stops; the stop
        # listener must write the recorder's buffer before closing storage.
        test = self

        class _StopsHomeAssistant(_FailingEnergyController):
            async def async_start(self) -> None:
                runtime = test.entry.runtime_data
                runtime.room_activity._buffer.append(
                    {
                        "t": 1_800_000_000,
                        "utc_offset_min": 180,
                        "room_id": "majlis",
                        "entity_id": "light.majlis",
                        "action": "off",
                        "source": "wall_switch",
                        "member_id": None,
                    }
                )
                (on_stop,) = [
                    handler
                    for event_type, handler in test.hass.bus.listeners
                    if event_type == "homeassistant_stop"
                ]
                await on_stop(None)
                self.closed = runtime.storage._conn is None
                raise RuntimeError("Home Assistant stopped")

        with mock.patch.object(integration, "EnergyController", _StopsHomeAssistant):
            with self.assertRaises(RuntimeError):
                await integration.async_setup_entry(self.hass, self.entry)
        self.entry.run_on_unload()
        self.assertTrue(self.entry.runtime_data.energy_controller.closed)

        storage = integration.HubStorage(self.data_dir / "hub.db")
        storage.open()
        self.addCleanup(storage.close)
        self.assertEqual(storage.room_activity().count(), 1)

    async def test_corrupt_identity_key_closes_storage(self) -> None:
        (self.data_dir / "identity_key.pem").write_text("not a key")
        with self.assertRaises(ConfigEntryError):
            await integration.async_setup_entry(self.hass, self.entry)
        self.entry.run_on_unload()
        self.assertIsNone(self.entry.runtime_data.storage._conn)
        self.assertEqual(self.hass.bus.listeners, [])

    async def test_engines_failing_to_load_close_the_database(self) -> None:
        opened = []

        class _Storage(integration.HubStorage):
            def open(self, *args, **kwargs):
                opened.append(self)
                super().open(*args, **kwargs)

        with (
            mock.patch.object(integration, "HubStorage", _Storage),
            mock.patch.object(
                integration.RegistryEngine,
                "warm_up",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            await integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0]._conn)


@unittest.skipIf(_SKIP, f"CasaSmart unimportable: {_SKIP}")
class PushIdentityUnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreadable_key_skips_push_with_a_warning(self) -> None:
        async def _executor(func, *args):
            return func(*args)

        hass = types.SimpleNamespace(async_add_executor_job=_executor)
        runtime = types.SimpleNamespace(
            tls=types.SimpleNamespace(),
            hub_config=H.FakeHubConfig(),
            relay_config_applied=None,
            push_dispatcher=None,
        )
        entry = types.SimpleNamespace(
            runtime_data=runtime,
            options={"push_relay_url": "https://relay.example.com"},
            data={},
        )
        denied = PermissionError(13, "Permission denied")
        with (
            mock.patch.object(integration, "ensure_push_identity", side_effect=denied),
            self.assertLogs(integration._LOGGER, logging.WARNING) as logs,
        ):
            await integration._async_start_push(hass, entry, Path("/nowhere"))
        self.assertIsNone(runtime.push_dispatcher)
        self.assertEqual([r.levelno for r in logs.records], [logging.WARNING])
        self.assertIn("Permission denied", logs.output[0])


if __name__ == "__main__":
    unittest.main()
