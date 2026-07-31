"""P3 tests for Energy Saving permissions, automation gating, and runtime order."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "custom_components"))
sys.path.insert(0, str(_ROOT / "custom_components" / "casasmart"))
sys.path.insert(0, str(_ROOT / "tests"))

from hastubs import install_casasmart_package, install_homeassistant_stubs  # noqa: E402

install_homeassistant_stubs()
install_casasmart_package()

from casasmart.auth_engine import AuthEngine  # noqa: E402
from casasmart.const import EVENT_ENERGY_CHANGED  # noqa: E402
from casasmart.energy import (  # noqa: E402
    LEVEL_LOW,
    EnergyEngine,
    default_level_config,
)
from casasmart.energy_runtime import (  # noqa: E402
    EnergyAutomationManager,
    EnergyController,
    EnergyFlags,
    energy_lockout_applies,
)
from homeassistant.core import State  # noqa: E402
from storage import HubStorage  # noqa: E402


class _States:
    def __init__(self, states=()) -> None:
        self.values = list(states)

    def async_all(self, domain=None):
        if domain is None:
            return list(self.values)
        return [item for item in self.values if item.domain == domain]


class _Services:
    def __init__(self) -> None:
        self.calls = []
        self.fail: set[tuple[str, str]] = set()

    async def async_call(self, domain, service, data, blocking=False):
        entity_id = data["entity_id"]
        self.calls.append((domain, service, entity_id, blocking))
        if (service, entity_id) in self.fail:
            raise RuntimeError("simulated failure")


class _Bus:
    def __init__(self) -> None:
        self.fired = []

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))


class _Hass:
    def __init__(self, states=()) -> None:
        self.states = _States(states)
        self.services = _Services()
        self.bus = _Bus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _Adapter:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.mode_stopped = 0
        self.applied = []

    def async_start(self):
        self.started += 1

    def async_stop(self):
        self.stopped += 1

    def async_mode_stopped(self):
        self.mode_stopped += 1

    async def async_apply(self, *, reason):
        self.applied.append(reason)
        return {"reason": reason, "commands": 0, "failures": 0, "issues": []}

    def issues(self):
        return []


class EnergyRuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = HubStorage(Path(self.tmp.name) / "hub.db")
        self.storage.open()
        self.addCleanup(self.storage.close)
        self.engine = EnergyEngine(
            self.storage.table("energy_configs"),
            self.storage.table("energy_state"),
            self.storage.energy_events(),
        )
        self.engine.warm_up()
        self.flags = EnergyFlags(self.storage.table("energy_flags"))

    def complete_low(self):
        config = default_level_config(LEVEL_LOW)
        config["setup_complete"] = True
        self.engine.replace_config(LEVEL_LOW, config)

    def test_permissions_match_the_canonical_role_matrix(self):
        for role in ("admin", "sub-admin", "user"):
            self.assertTrue(AuthEngine.authorize({"role": role}, "energy.read"))
        for permission in ("energy.control", "energy.manage"):
            self.assertTrue(AuthEngine.authorize({"role": "admin"}, permission))
            self.assertFalse(
                AuthEngine.authorize({"role": "sub-admin"}, permission)
            )
            self.assertFalse(AuthEngine.authorize({"role": "user"}, permission))

    def test_flags_default_false_validate_persist_and_delete(self):
        key = "casa_automation_arrival"
        self.assertFalse(self.flags.works_during_energy_saving(key))
        self.assertTrue(self.flags.set_works_during_energy_saving(key, True))
        self.assertTrue(self.flags.works_during_energy_saving(key))
        with self.assertRaises(ValueError):
            self.flags.set_works_during_energy_saving(key, "yes")
        self.flags.delete_automation(key)
        self.assertFalse(self.flags.works_during_energy_saving(key))

    def test_lockout_is_active_for_non_admin_only(self):
        self.complete_low()
        self.engine.activate(LEVEL_LOW)
        self.assertTrue(energy_lockout_applies(self.engine, {"role": "user"}))
        self.assertTrue(
            energy_lockout_applies(self.engine, {"role": "sub-admin"})
        )
        self.assertFalse(energy_lockout_applies(self.engine, {"role": "admin"}))
        self.engine.deactivate()
        self.assertFalse(energy_lockout_applies(self.engine, {"role": "user"}))

    async def test_disables_unflagged_and_restores_exact_successful_set(self):
        states = [
            State("automation.blocked", "on", {"id": "blocked_key"}),
            State("automation.allowed", "on", {"id": "allowed_key"}),
            State("automation.already_off", "off", {"id": "off_key"}),
        ]
        hass = _Hass(states)
        self.flags.set_works_during_energy_saving("allowed_key", True)
        manager = EnergyAutomationManager(hass, self.engine, self.flags)

        await manager.async_enforce_active()
        self.assertEqual(
            hass.services.calls,
            [("automation", "turn_off", "automation.blocked", True)],
        )
        self.assertEqual(
            self.flags.disabled_automations(), ["automation.blocked"]
        )

        await manager.async_restore()
        self.assertEqual(
            hass.services.calls[-1],
            ("automation", "turn_on", "automation.blocked", True),
        )
        self.assertEqual(self.flags.disabled_automations(), [])

    async def test_restore_failure_stays_durable_for_startup_retry(self):
        hass = _Hass()
        self.flags.set_disabled_automations(["automation.retry_me"])
        hass.services.fail.add(("turn_on", "automation.retry_me"))
        manager = EnergyAutomationManager(hass, self.engine, self.flags)
        await manager.async_restore()
        self.assertEqual(
            self.flags.disabled_automations(), ["automation.retry_me"]
        )

        hass.services.fail.clear()
        await manager.async_restore()
        self.assertEqual(self.flags.disabled_automations(), [])

    async def test_controller_orders_activation_reapply_and_deactivation(self):
        self.complete_low()
        hass = _Hass()
        adapter = _Adapter()
        manager = EnergyAutomationManager(hass, self.engine, self.flags)
        controller = EnergyController(hass, self.engine, adapter, manager)

        await controller.async_start()
        self.assertEqual(adapter.started, 1)
        await controller.async_activate(LEVEL_LOW, actor="owner")
        self.assertEqual(adapter.applied, ["activation"])
        await controller.async_reapply(actor="owner")
        self.assertEqual(adapter.applied, ["activation", "reapply"])
        state = await controller.async_deactivate(actor="owner")
        self.assertFalse(state["active"])
        self.assertEqual(adapter.mode_stopped, 1)
        self.assertEqual(
            [kind for kind, _data in hass.bus.fired],
            [EVENT_ENERGY_CHANGED, EVENT_ENERGY_CHANGED, EVENT_ENERGY_CHANGED],
        )


if __name__ == "__main__":
    unittest.main()
