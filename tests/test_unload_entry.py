"""Unloading the config entry: all or nothing, and the result tells which.

Home Assistant runs the entry's on-unload callbacks and drops runtime_data
only when ``async_unload_entry`` returns True. So when a platform fails to
unload, the hub must report False and leave its runtimes and storage alone
(the remaining entities still use them); when the platforms unload, every
runtime stops and storage closes last.

The function under test is the real one from ``__init__.py``; the runtimes
and ``hass`` are mocks. Needs a real Home Assistant.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H

try:
    integration = H.import_integration()
    _ERR = None
except Exception as err:
    _ERR = err

# Runtimes with a synchronous stop, then those with an async one.
_SYNC_STOPS = {
    "suggestions": "stop",
    "alarm_adapter": "async_stop",
    "tank_push_monitor": "async_stop",
    "relay_registrar": "stop",
    "push_dispatcher": "async_stop",
}
_ASYNC_STOPS = ("energy_controller", "athan_scheduler", "audio_adapter", "mdns", "tls")


@unittest.skipIf(_ERR, f"CasaSmart unimportable: {_ERR}")
class UnloadEntryTests(unittest.IsolatedAsyncioTestCase):
    def _hub(self, platforms_unloaded: bool):
        """A hass and entry whose platform unload returns ``platforms_unloaded``."""
        calls = mock.Mock()  # one parent, so the call order is recorded
        runtime = types.SimpleNamespace(storage=calls.storage)
        for name, method in _SYNC_STOPS.items():
            setattr(runtime, name, getattr(calls, name))
            getattr(calls, name).attach_mock(mock.Mock(), method)
        for name in _ASYNC_STOPS:
            setattr(runtime, name, getattr(calls, name))
            getattr(calls, name).attach_mock(mock.AsyncMock(), "async_stop")

        async def _unload_platforms(entry, platforms):
            calls.unload_platforms(platforms)
            return platforms_unloaded

        async def _executor(func, *args):
            return func(*args)

        hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(
                async_unload_platforms=_unload_platforms
            ),
            async_add_executor_job=_executor,
            data={},
        )
        return hass, types.SimpleNamespace(runtime_data=runtime), calls

    async def test_failed_platform_unload_reports_false_and_tears_nothing_down(
        self,
    ):
        hass, entry, calls = self._hub(platforms_unloaded=False)
        self.assertIs(await integration.async_unload_entry(hass, entry), False)
        self.assertEqual(
            [c[0] for c in calls.mock_calls], ["unload_platforms"], calls.mock_calls
        )

    async def test_unloaded_platforms_stop_everything_and_close_storage_last(self):
        hass, entry, calls = self._hub(platforms_unloaded=True)
        self.assertIs(await integration.async_unload_entry(hass, entry), True)
        names = [c[0] for c in calls.mock_calls]
        self.assertEqual(names[0], "unload_platforms")
        self.assertEqual(names[-1], "storage.close")
        for name, method in _SYNC_STOPS.items():
            self.assertIn(f"{name}.{method}", names)
        for name in _ASYNC_STOPS:
            self.assertIn(f"{name}.async_stop", names)


if __name__ == "__main__":
    unittest.main()
