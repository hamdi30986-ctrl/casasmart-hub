"""The reset buttons follow the same admin rule as the reset services.

factory_reset and set_tunnel_url are admin-only services, so a non-admin Home
Assistant user must not reach the same wipes through the button entities.
Presses without a user (automations, scripts) are allowed, as for the
services. Needs a real Home Assistant.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view_harness as H


def _load_button(package):
    """Import the real button platform against the real package module."""
    saved = {name: sys.modules.get(name) for name in ("casasmart", "casasmart.button")}
    sys.modules["casasmart"] = package
    sys.modules.pop("casasmart.button", None)
    try:
        return importlib.import_module("casasmart.button")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


try:
    from homeassistant.core import Context
    from homeassistant.exceptions import HomeAssistantError, Unauthorized

    button = _load_button(H.import_integration())
    _ERR: Exception | None = None
except Exception as err:
    _ERR = err

_SKIP = H.IMPORT_ERROR or _ERR


class _Hass:
    def __init__(self, users: dict[str, bool]) -> None:
        self._users = users
        self.services = types.SimpleNamespace(async_call=mock.AsyncMock())
        self.bus = types.SimpleNamespace(async_fire=mock.Mock())
        self.config_entries = types.SimpleNamespace(async_schedule_reload=mock.Mock())
        self.auth = types.SimpleNamespace(async_get_user=self._get_user)
        self.jobs = 0

    async def _get_user(self, user_id: str):
        if user_id not in self._users:
            return None
        return types.SimpleNamespace(id=user_id, is_admin=self._users[user_id])

    async def async_add_executor_job(self, func, *args):
        self.jobs += 1
        return func(*args)


def _entry(*, fail: bool = False) -> types.SimpleNamespace:
    def wipe():
        if fail:
            raise OSError("disk I/O error")
        return ["dev-1"]

    table = types.SimpleNamespace(clear=mock.Mock())
    runtime = types.SimpleNamespace(
        auth=types.SimpleNamespace(wipe_all_devices=wipe),
        pairing=types.SimpleNamespace(
            clear_all_codes=lambda: 1, ensure_bootstrap_code=lambda: "ABCD2345"
        ),
        storage=types.SimpleNamespace(table=lambda name: table),
        hub_config=types.SimpleNamespace(set=mock.Mock()),
    )
    return types.SimpleNamespace(entry_id="entry-1", runtime_data=runtime)


@unittest.skipIf(_SKIP, f"CasaSmart unimportable: {_SKIP}")
class ResetButtonAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(button.persistent_notification, "async_create")
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

    def _press_context(self, entity, user_id: str | None) -> None:
        entity._context = Context(user_id=user_id)

    async def test_factory_reset_button_refuses_a_non_admin(self) -> None:
        hass = _Hass({"admin": True, "user": False})
        entity = button.CasaSmartFactoryResetButton(hass, _entry())
        self._press_context(entity, "user")
        with self.assertRaises(Unauthorized):
            await entity.async_press()
        hass.services.async_call.assert_not_called()
        self.notify.assert_not_called()

    async def test_factory_reset_button_passes_the_presser_on(self) -> None:
        hass = _Hass({"admin": True})
        entity = button.CasaSmartFactoryResetButton(hass, _entry())
        self._press_context(entity, "admin")
        await entity.async_press()
        kwargs = hass.services.async_call.await_args.kwargs
        self.assertEqual(kwargs.get("context").user_id, "admin")

    async def test_regenerate_button_refuses_a_non_admin(self) -> None:
        hass = _Hass({"user": False})
        entity = button.CasaSmartRegeneratePairingButton(hass, _entry())
        self._press_context(entity, "user")
        with self.assertRaises(Unauthorized):
            await entity.async_press()
        self.assertEqual(hass.jobs, 0)

    async def test_buttons_allow_a_press_without_a_user(self) -> None:
        hass = _Hass({})
        regenerate = button.CasaSmartRegeneratePairingButton(hass, _entry())
        self._press_context(regenerate, None)
        await regenerate.async_press()
        self.assertEqual(hass.jobs, 1)
        reset = button.CasaSmartFactoryResetButton(hass, _entry())
        self._press_context(reset, None)
        await reset.async_press()
        hass.services.async_call.assert_awaited()

    async def test_failed_regenerate_reloads_so_memory_matches_storage(self) -> None:
        hass = _Hass({"admin": True})
        entity = button.CasaSmartRegeneratePairingButton(hass, _entry(fail=True))
        self._press_context(entity, "admin")
        with self.assertRaises(HomeAssistantError):
            await entity.async_press()
        hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")
        hass.bus.async_fire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
