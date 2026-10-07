"""Tests for Energy Saving permissions, automation gating, and runtime order."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

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
    EnergyInactiveError,
    default_level_config,
)
from casasmart.energy_adapter import EnergyAdapter  # noqa: E402
from casasmart.energy_runtime import (  # noqa: E402
    EnergyAutomationManager,
    EnergyController,
    EnergyFlags,
    energy_lockout_applies,
    energy_lockout_refusal,
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

    def set(self, entity_id, state):
        self.values = [
            State(entity_id, state, dict(item.attributes))
            if item.entity_id == entity_id
            else item
            for item in self.values
        ]

    def state_of(self, entity_id):
        return next(item.state for item in self.values if item.entity_id == entity_id)


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

    def async_listen(self, _event_type, _callback):
        return lambda: None


class _Hass:
    def __init__(self, states=()) -> None:
        self.states = _States(states)
        self.services = _Services()
        self.bus = _Bus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_background_task(self, target, name, eager_start=True):
        # Eager by default, as in HA: the coroutine runs to its first await.
        return asyncio.Task(
            target, loop=asyncio.get_running_loop(), name=name, eager_start=eager_start
        )


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


class _Steps:
    """Number every awaited HA call and hold one of them open on request."""

    def __init__(self) -> None:
        self.count = 0
        self.labels: list[str] = []
        self.hold_at: int | None = None
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    def hold(self, offset: int) -> None:
        """Hold the ``offset``-th call from now (0 = the next one)."""
        self.hold_at = self.count + offset

    async def step(self, label: str) -> None:
        index = self.count
        self.count += 1
        self.labels.append(label)
        if index == self.hold_at:
            self.held.set()
            await self.release.wait()


class _SteppedServices(_Services):
    """Service calls that take effect only once their step completes."""

    def __init__(self, states, steps) -> None:
        super().__init__()
        self._states = states
        self._steps = steps

    async def async_call(self, domain, service, data, blocking=False):
        entity_id = data["entity_id"]
        self.calls.append((domain, service, entity_id, blocking))
        await self._steps.step(f"{domain}.{service} {entity_id}")
        self._states.set(entity_id, "on" if service == "turn_on" else "off")


class _SteppedHass(_Hass):
    """Executor jobs and service calls both go through one ``_Steps``."""

    def __init__(self, states=()) -> None:
        super().__init__(states)
        self.steps = _Steps()
        self.services = _SteppedServices(self.states, self.steps)

    async def async_add_executor_job(self, func, *args):
        await self.steps.step(getattr(func, "__name__", "job"))
        return func(*args)


class _Rooms:
    def __init__(self, rooms) -> None:
        self.rooms = rooms

    def room_of(self, entity_id):
        return self.rooms.get(entity_id)

    def list_user_devices(self):
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
            self.assertFalse(AuthEngine.authorize({"role": "sub-admin"}, permission))
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
        self.assertTrue(energy_lockout_applies(self.engine, {"role": "sub-admin"}))
        self.assertFalse(energy_lockout_applies(self.engine, {"role": "admin"}))
        self.engine.deactivate()
        self.assertFalse(energy_lockout_applies(self.engine, {"role": "user"}))

    def test_lockout_refusal_carries_its_code(self):
        # The phone keeps "code" on a 403: it tells the lockout apart from a
        # credential that a re-login would fix.
        body = energy_lockout_refusal()
        self.assertEqual(body["error"], "energy_lockout")
        self.assertEqual(body["code"], "energy_lockout")
        self.assertIsInstance(body["message"], str)

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
        self.assertEqual(self.flags.disabled_automations(), ["automation.blocked"])

        await manager.async_restore()
        self.assertEqual(
            hass.services.calls[-1],
            ("automation", "turn_on", "automation.blocked", True),
        )
        self.assertEqual(self.flags.disabled_automations(), [])

    async def test_automation_with_an_overlong_id_is_skipped(self):
        # HA accepts any id length; the flag store caps keys at 255 characters.
        states = [
            State("automation.long", "on", {"id": "x" * 256}),
            State("automation.short", "on", {"id": "short_key"}),
        ]
        hass = _Hass(states)
        manager = EnergyAutomationManager(hass, self.engine, self.flags)

        with self.assertLogs("casasmart.energy_runtime", level="WARNING"):
            await manager.async_enforce_active()
        self.assertEqual(
            hass.services.calls,
            [("automation", "turn_off", "automation.short", True)],
        )
        self.assertEqual(self.flags.disabled_automations(), ["automation.short"])

    async def test_restore_failure_stays_durable_for_startup_retry(self):
        hass = _Hass()
        self.flags.set_disabled_automations(["automation.retry_me"])
        hass.services.fail.add(("turn_on", "automation.retry_me"))
        manager = EnergyAutomationManager(hass, self.engine, self.flags)
        await manager.async_restore()
        self.assertEqual(self.flags.disabled_automations(), ["automation.retry_me"])

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

    # -- overlapping transitions ------------------------------------------

    def home(self):
        """A fresh home: real engine, flags, manager and adapter over stepped HA.

        Low turns off two unflagged automations and one configured plug.
        """
        storage = HubStorage(Path(tempfile.mkdtemp(dir=self.tmp.name)) / "hub.db")
        storage.open()
        self.addCleanup(storage.close)
        engine = EnergyEngine(
            storage.table("energy_configs"),
            storage.table("energy_state"),
            storage.energy_events(),
        )
        engine.warm_up()
        config = default_level_config(LEVEL_LOW)
        config.update(setup_complete=True, plug_offs=["switch.plug"])
        engine.replace_config(LEVEL_LOW, config)
        flags = EnergyFlags(storage.table("energy_flags"))
        hass = _SteppedHass(
            [
                State("automation.a", "on", {"id": "a"}),
                State("automation.b", "on", {"id": "b"}),
                State("switch.plug", "on"),
            ]
        )
        adapter = EnergyAdapter(
            hass,
            engine,
            _Rooms({"switch.plug": "den"}),
            area_resolver=lambda _hass, _entity_id: None,
            category_resolver=lambda _hass, _entity_id: None,
        )
        manager = EnergyAutomationManager(hass, engine, flags)
        return EnergyController(hass, engine, adapter, manager), hass, flags

    async def deactivate_while_held(self, controller, hass, transition, offset):
        """Hold ``transition``'s ``offset``-th HA call, deactivate, then let go.

        Returns the deactivated state, the transition's outcome, and every
        service call made after the deactivate had returned.
        """
        hass.steps.hold(offset)
        running = asyncio.create_task(transition)
        await asyncio.wait_for(hass.steps.held.wait(), 1)
        deactivating = asyncio.create_task(controller.async_deactivate(actor="owner"))
        await asyncio.sleep(0)
        hass.steps.release.set()
        state = await asyncio.wait_for(deactivating, 1)
        done = len(hass.services.calls)
        (outcome,) = await asyncio.gather(running, return_exceptions=True)
        await asyncio.sleep(0)
        return state, outcome, hass.services.calls[done:]

    def assert_deactivate_won(self, controller, hass, flags, state, late):
        self.assertFalse(state["active"])
        self.assertIsNone(controller.engine.active_level)
        self.assertEqual(late, [])
        self.assertEqual(flags.disabled_automations(), [])
        self.assertEqual(hass.states.state_of("automation.a"), "on")
        self.assertEqual(hass.states.state_of("automation.b"), "on")

    async def test_single_activation_is_unaffected(self):
        controller, hass, flags = self.home()
        result = await controller.async_activate(LEVEL_LOW, actor="owner")
        self.assertEqual(result["state"]["active_level"], LEVEL_LOW)
        self.assertEqual(
            (result["apply"]["commands"], result["apply"]["failures"]), (1, 0)
        )
        self.assertEqual(
            hass.services.calls,
            [
                ("automation", "turn_off", "automation.a", True),
                ("automation", "turn_off", "automation.b", True),
                ("switch", "turn_off", "switch.plug", True),
            ],
        )
        self.assertEqual(flags.disabled_automations(), ["automation.a", "automation.b"])
        self.assertEqual(hass.bus.fired, [(EVENT_ENERGY_CHANGED, None)])

    async def test_activate_then_deactivate_restores_every_automation(self):
        controller, hass, flags = self.home()
        await controller.async_activate(LEVEL_LOW, actor="owner")
        state = await controller.async_deactivate(actor="owner")
        self.assert_deactivate_won(controller, hass, flags, state, [])
        self.assertEqual(hass.states.state_of("switch.plug"), "off")

    async def test_deactivate_wins_over_activation_at_every_awaited_step(self):
        dry, dry_hass, _flags = self.home()
        await dry.async_activate(LEVEL_LOW)
        for offset, label in enumerate(dry_hass.steps.labels):
            with self.subTest(step=offset, call=label):
                controller, hass, flags = self.home()
                state, outcome, late = await self.deactivate_while_held(
                    controller,
                    hass,
                    controller.async_activate(LEVEL_LOW, actor="owner"),
                    offset,
                )
                self.assert_deactivate_won(controller, hass, flags, state, late)
                self.assertIsInstance(outcome, EnergyInactiveError)

    async def test_deactivate_wins_over_reapply_at_every_awaited_step(self):
        dry, dry_hass, _flags = self.home()
        await dry.async_activate(LEVEL_LOW)
        before = dry_hass.steps.count
        await dry.async_reapply()
        for offset, label in enumerate(dry_hass.steps.labels[before:]):
            with self.subTest(step=offset, call=label):
                controller, hass, flags = self.home()
                await controller.async_activate(LEVEL_LOW, actor="owner")
                state, outcome, late = await self.deactivate_while_held(
                    controller, hass, controller.async_reapply(actor="owner"), offset
                )
                self.assert_deactivate_won(controller, hass, flags, state, late)
                self.assertIsInstance(outcome, EnergyInactiveError)

    @staticmethod
    async def started(controller):
        """Start the controller and wait for its background startup pass."""
        await controller.async_start()
        await controller._startup

    async def test_deactivate_wins_over_startup_apply_at_every_awaited_step(self):
        dry, dry_hass, _flags = self.home()
        dry.engine.activate(LEVEL_LOW)
        await self.started(dry)
        for offset, label in enumerate(dry_hass.steps.labels):
            with self.subTest(step=offset, call=label):
                controller, hass, flags = self.home()
                controller.engine.activate(LEVEL_LOW)
                state, outcome, late = await self.deactivate_while_held(
                    controller, hass, self.started(controller), offset
                )
                self.assert_deactivate_won(controller, hass, flags, state, late)
                self.assertIsNone(outcome)

    async def test_setup_does_not_wait_for_the_startup_apply(self):
        dry, dry_hass, _flags = self.home()
        dry.engine.activate(LEVEL_LOW)
        await self.started(dry)
        controller, hass, _flags = self.home()
        controller.engine.activate(LEVEL_LOW)
        hass.steps.hold(dry_hass.steps.labels.index("switch.turn_off switch.plug"))

        # The plug never answers; setup must finish anyway.
        await asyncio.wait_for(controller.async_start(), 1)
        await asyncio.wait_for(hass.steps.held.wait(), 1)
        await asyncio.wait_for(controller.async_stop(), 1)
        self.assertTrue(controller._startup.done())

    async def test_stop_cancels_the_device_pass_in_flight(self):
        dry, dry_hass, _flags = self.home()
        await dry.async_activate(LEVEL_LOW)
        controller, hass, flags = self.home()
        hass.steps.hold(dry_hass.steps.labels.index("switch.turn_off switch.plug"))
        activating = asyncio.create_task(
            controller.async_activate(LEVEL_LOW, actor="owner")
        )
        await asyncio.wait_for(hass.steps.held.wait(), 1)
        done = len(hass.services.calls)

        # An unload or reload must not leave the old pass running.
        await asyncio.wait_for(controller.async_stop(), 1)
        with self.assertRaises(EnergyInactiveError):
            await asyncio.wait_for(activating, 1)
        hass.steps.release.set()
        await asyncio.sleep(0)
        self.assertEqual(hass.services.calls[done:], [])
        self.assertEqual(hass.states.state_of("switch.plug"), "on")
        # The level stays active, so the next start applies it again.
        self.assertEqual(controller.engine.active_level, LEVEL_LOW)
        self.assertEqual(flags.disabled_automations(), ["automation.a", "automation.b"])

    async def test_deactivate_does_not_wait_for_a_hung_device_command(self):
        dry, dry_hass, _flags = self.home()
        await dry.async_activate(LEVEL_LOW)
        controller, hass, flags = self.home()
        hass.steps.hold(dry_hass.steps.labels.index("switch.turn_off switch.plug"))
        activating = asyncio.create_task(
            controller.async_activate(LEVEL_LOW, actor="owner")
        )
        await asyncio.wait_for(hass.steps.held.wait(), 1)

        # The plug never answers; the deactivate must not wait for it.
        state = await asyncio.wait_for(controller.async_deactivate(actor="owner"), 1)
        self.assert_deactivate_won(controller, hass, flags, state, [])
        with self.assertRaises(EnergyInactiveError):
            await asyncio.wait_for(activating, 1)
        hass.steps.release.set()
        await asyncio.sleep(0)
        self.assertEqual(hass.states.state_of("switch.plug"), "on")

    async def test_deactivate_stops_the_automation_pass_at_the_next_step(self):
        dry, dry_hass, _flags = self.home()
        await dry.async_activate(LEVEL_LOW)
        controller, hass, flags = self.home()
        state, outcome, late = await self.deactivate_while_held(
            controller,
            hass,
            controller.async_activate(LEVEL_LOW, actor="owner"),
            dry_hass.steps.labels.index("automation.turn_off automation.a"),
        )
        self.assert_deactivate_won(controller, hass, flags, state, late)
        self.assertIsInstance(outcome, EnergyInactiveError)
        # The in-flight turn_off finished and was undone; nothing else ran.
        self.assertEqual(
            hass.services.calls,
            [
                ("automation", "turn_off", "automation.a", True),
                ("automation", "turn_on", "automation.a", True),
            ],
        )

    async def test_activation_queued_behind_a_newer_deactivate_never_starts(self):
        controller, hass, flags = self.home()
        hass.steps.hold(0)
        first = asyncio.create_task(controller.async_deactivate(actor="owner"))
        await asyncio.wait_for(hass.steps.held.wait(), 1)
        activating = asyncio.create_task(
            controller.async_activate(LEVEL_LOW, actor="owner")
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(controller.async_deactivate(actor="owner"))
        await asyncio.sleep(0)

        hass.steps.release.set()
        await first
        with self.assertRaises(EnergyInactiveError):
            await activating
        state = await second
        self.assert_deactivate_won(controller, hass, flags, state, [])
        self.assertEqual(controller.engine.recent_events(kinds=["activated"]), [])
        self.assertEqual(hass.services.calls, [])

    async def test_reapply_waits_for_the_activation_in_flight(self):
        dry, dry_hass, _flags = self.home()
        await dry.async_activate(LEVEL_LOW)
        controller, hass, flags = self.home()
        hass.steps.hold(dry_hass.steps.labels.index("automation.turn_off automation.a"))
        activating = asyncio.create_task(
            controller.async_activate(LEVEL_LOW, actor="owner")
        )
        await asyncio.wait_for(hass.steps.held.wait(), 1)
        reached = hass.steps.count
        reapplying = asyncio.create_task(controller.async_reapply(actor="owner"))
        await asyncio.sleep(0)
        self.assertEqual(hass.steps.count, reached)

        hass.steps.release.set()
        await activating
        result = await reapplying
        self.assertEqual(result["state"]["active_level"], LEVEL_LOW)
        self.assertEqual(
            [call for call in hass.services.calls if call[0] == "automation"],
            [
                ("automation", "turn_off", "automation.a", True),
                ("automation", "turn_off", "automation.b", True),
            ],
        )
        self.assertEqual(flags.disabled_automations(), ["automation.a", "automation.b"])


if __name__ == "__main__":
    unittest.main()
