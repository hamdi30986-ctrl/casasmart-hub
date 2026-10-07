"""The cloudflared add-on across options changes and integration removal.

While a Cloudflare domain is set, the reconciler owns the add-on's boot mode:
tunnel OFF parks it stopped at boot=manual. Clearing the domain makes the
reconciler (and removal) inert, so the domain clear itself has to hand
auto-boot back, or the add-on stays at boot=manual for good.

The functions under test are the real ones from ``__init__.py``; the add-on
controller and ``hass`` are fakes. Needs a real Home Assistant.
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
    DOMAIN_KEY = integration.CONF_CLOUDFLARE_DOMAIN
    ENABLED_KEY = integration.CONF_TUNNEL_ENABLED
    TunnelControlError = integration.TunnelControlError

    _ERR = None
except Exception as err:
    _ERR = err

SLUG = "abc123_cloudflared"
HUB_DOMAIN = "hub.example.com"


class FakeController:
    """The CloudflaredController surface the reconciler uses, over a dict."""

    def __init__(self, *, running=True, boot="auto", slug=SLUG, available=True):
        self.addon = {"running": running, "boot": boot}
        self.slug = slug
        self.is_available = available
        self.restore_error: Exception | None = None
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.is_available

    async def async_discover(self):
        return self.slug

    async def async_state(self, slug):
        return types.SimpleNamespace(slug=slug, **self.addon)

    async def async_enable(self, slug, *, running):
        self.calls.append("enable")
        self.addon.update(running=True, boot="auto")

    async def async_disable(self, slug, *, running):
        self.calls.append("disable")
        self.addon.update(running=False, boot="manual")

    async def async_restore_boot_auto(self, slug):
        self.calls.append("restore_boot_auto")
        if self.restore_error is not None:
            raise self.restore_error
        self.addon["boot"] = "auto"


class FakeHass:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(components=set())  # no Supervisor

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeEntry:
    def __init__(self, options, controller) -> None:
        self.options = dict(options)
        self.data: dict = {}
        self.runtime_data = types.SimpleNamespace(
            relay_config_applied=None,
            hub_config=H.FakeHubConfig(),
            tunnel_options_applied=None,
            tunnel_control=controller,
        )
        self._tasks: list = []

    def async_create_background_task(self, hass, coro, name=None):
        self._tasks.append(coro)

    async def run_tasks(self) -> None:
        while self._tasks:
            await self._tasks.pop(0)


@unittest.skipIf(_ERR, f"CasaSmart unimportable: {_ERR}")
class TunnelDomainClearTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        notifications = mock.patch.object(
            integration, "persistent_notification", mock.MagicMock()
        )
        notifications.start()
        self.addCleanup(notifications.stop)
        self.hass = FakeHass()

    async def _boot(self, controller, **options) -> FakeEntry:
        """What setup does: derive the advertised URL, then reconcile."""
        entry = FakeEntry(options, controller)
        await integration._async_sync_tunnel_url(self.hass, entry)
        await integration._async_reconcile_tunnel(self.hass, entry)
        return entry

    async def _change_options(self, entry, **options) -> None:
        entry.options = dict(options)
        await integration._async_options_updated(self.hass, entry)
        await entry.run_tasks()

    async def test_clear_after_tunnel_off_restores_boot_auto(self) -> None:
        controller = FakeController(running=True, boot="auto")
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: True}
        )
        await self._change_options(
            entry, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: False}
        )
        self.assertEqual(controller.addon, {"running": False, "boot": "manual"})

        # The options flow drops both keys when the domain is cleared.
        await self._change_options(entry)

        self.assertEqual(controller.addon, {"running": False, "boot": "auto"})
        self.assertEqual(controller.calls, ["disable", "restore_boot_auto"])
        self.assertIsNone(entry.runtime_data.hub_config.get("tunnel_url"))
        # Removal afterwards has nothing left to undo.
        with mock.patch.object(integration, "CloudflaredController") as built:
            await integration.async_remove_entry(self.hass, entry)
        built.assert_not_called()

    async def test_clear_while_on_does_not_start_the_add_on(self) -> None:
        controller = FakeController(running=True, boot="auto")
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: True}
        )
        controller.addon["running"] = False  # e.g. it crashed meanwhile

        await self._change_options(entry)

        self.assertNotIn("enable", controller.calls)
        self.assertEqual(controller.addon, {"running": False, "boot": "auto"})

    async def test_clear_without_supervisor_is_a_quiet_no_op(self) -> None:
        # The real controller, on a Home Assistant without a Supervisor.
        controller = integration.CloudflaredController(self.hass)
        self.assertFalse(controller.available())
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: False}
        )
        await self._change_options(entry)  # must not raise
        self.assertIsNone(entry.runtime_data.hub_config.get("tunnel_url"))

    async def test_clear_without_the_add_on_is_a_no_op(self) -> None:
        controller = FakeController(slug=None)
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: False}
        )
        await self._change_options(entry)
        self.assertEqual(controller.calls, [])

    async def test_clear_with_a_supervisor_error_only_logs(self) -> None:
        controller = FakeController(running=False, boot="manual")
        controller.restore_error = TunnelControlError("Supervisor unreachable")
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: False}
        )
        with self.assertLogs(integration.__name__, "WARNING") as logs:
            await self._change_options(entry)  # must not raise
        self.assertIn("Supervisor unreachable", "\n".join(logs.output))
        self.assertEqual(controller.calls, ["restore_boot_auto"])

    async def test_reconcile_with_a_domain_is_unchanged(self) -> None:
        controller = FakeController(running=True, boot="auto")
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: False}
        )
        self.assertEqual(controller.calls, ["disable"])
        await self._change_options(entry, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: True})
        self.assertEqual(controller.addon, {"running": True, "boot": "auto"})
        # Moving to another domain is not a clear: the reconciler stays in charge.
        await self._change_options(
            entry, **{DOMAIN_KEY: "other.example.com", ENABLED_KEY: False}
        )
        self.assertEqual(controller.calls, ["disable", "enable", "disable"])
        self.assertEqual(controller.addon, {"running": False, "boot": "manual"})

    async def test_removal_with_a_domain_still_restores_boot_auto(self) -> None:
        controller = FakeController(running=True, boot="auto")
        entry = await self._boot(
            controller, **{DOMAIN_KEY: HUB_DOMAIN, ENABLED_KEY: False}
        )
        with mock.patch.object(
            integration, "CloudflaredController", return_value=controller
        ):
            await integration.async_remove_entry(self.hass, entry)
        self.assertEqual(controller.calls, ["disable", "restore_boot_auto"])
        self.assertEqual(controller.addon, {"running": False, "boot": "auto"})

    async def test_never_configured_domain_never_touches_the_add_on(self) -> None:
        controller = FakeController(running=False, boot="manual")
        entry = await self._boot(controller)
        await self._change_options(entry)
        self.assertEqual(controller.calls, [])
        self.assertEqual(controller.addon, {"running": False, "boot": "manual"})


if __name__ == "__main__":
    unittest.main()
