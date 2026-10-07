"""Energy Saving runtime: the controller, automations and the lockout check.

EnergyController runs start-up, activate, deactivate and re-apply one at a
time around the engine (energy) and the device adapter (energy_adapter).
EnergyAutomationManager switches unflagged HA automations off while a level
is active and back on afterwards; EnergyFlags stores the flags and which
automations the hub switched off. energy_lockout_applies is the check that
stops non-admins overriding an active level from any command path, and
energy_lockout_refusal the body of that 403.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant

from .auth_tokens import ROLE_ADMIN
from .automations import MAX_AUTOMATION_KEY_LENGTH
from .const import EVENT_ENERGY_CHANGED
from .energy import EnergyEngine, EnergyInactiveError
from .energy_adapter import EnergyAdapter

_LOGGER = logging.getLogger(__name__)

# Flag rows are keyed "automation:<config key>"; the remembered set of
# automations the hub switched off lives under one reserved key.
_FLAG_PREFIX = "automation:"
_DISABLED_KEY = "_disabled_automations"

EVENT_AUTOMATION_DISABLED = "automation_disabled"
EVENT_AUTOMATION_RESTORED = "automation_restored"
EVENT_AUTOMATION_DISABLE_FAILED = "automation_disable_failed"
EVENT_AUTOMATION_RESTORE_FAILED = "automation_restore_failed"


def _clean_automation_ids(items: Any) -> list[str]:
    """The stripped automation entity ids among the items, sorted, each once."""
    return sorted(
        {
            item.strip()
            for item in items
            if isinstance(item, str) and item.strip().startswith("automation.")
        }
    )


def energy_lockout_applies(engine: EnergyEngine, claims: dict[str, Any]) -> bool:
    """True when the caller is non-admin and the active level locks control."""
    state = engine.snapshot()
    return bool(
        state["active"]
        and state["lockout_enabled"]
        and claims.get("role") != ROLE_ADMIN
    )


def energy_lockout_refusal() -> dict[str, str]:
    """The 403 body for a command the lockout refuses.

    The phone reads code on a 403: this isn't an expired login.
    """
    return {
        "error": "energy_lockout",
        "message": "Energy saving is active — controls are locked by the admin",
        "code": "energy_lockout",
    }


class EnergyFlags:
    """Automation flags, and the automations the hub switched off.

    Methods are synchronous (the table is SQLite); run them in the executor.
    """

    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _clean_key(config_key: Any) -> str:
        """A stripped, non-empty, bounded automation config key."""
        if not isinstance(config_key, str) or not config_key.strip():
            raise ValueError("automation config key must be a non-empty string")
        key = config_key.strip()
        if len(key) > MAX_AUTOMATION_KEY_LENGTH:
            raise ValueError(
                f"automation config key must be <= {MAX_AUTOMATION_KEY_LENGTH} characters"
            )
        return key

    def works_during_energy_saving(self, config_key: str) -> bool:
        """True when the automation may keep running while a level is active."""
        key = self._clean_key(config_key)
        value = self._table.get(f"{_FLAG_PREFIX}{key}")
        return bool(
            isinstance(value, dict) and value.get("works_during_energy_saving") is True
        )

    def set_works_during_energy_saving(self, config_key: str, enabled: Any) -> bool:
        """Store the automation's flag and return it."""
        key = self._clean_key(config_key)
        if not isinstance(enabled, bool):
            raise ValueError("works_during_energy_saving must be a boolean")
        self._table[f"{_FLAG_PREFIX}{key}"] = {"works_during_energy_saving": enabled}
        return enabled

    def delete_automation(self, config_key: str) -> None:
        """Forget the flag of an automation that no longer exists."""
        key = self._clean_key(config_key)
        self._table.pop(f"{_FLAG_PREFIX}{key}", None)

    def disabled_automations(self) -> list[str]:
        """Entity ids the hub switched off and still owes a switch-on."""
        value = self._table.get(_DISABLED_KEY)
        if not isinstance(value, dict) or not isinstance(value.get("entity_ids"), list):
            return []
        return _clean_automation_ids(value["entity_ids"])

    def set_disabled_automations(self, entity_ids: list[str]) -> None:
        """Replace the remembered set; an empty set removes the row."""
        clean = _clean_automation_ids(entity_ids)
        if clean:
            self._table[_DISABLED_KEY] = {"entity_ids": clean}
        else:
            self._table.pop(_DISABLED_KEY, None)


class EnergyAutomationManager:
    """Switch unflagged automations off, and later back on only those."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: EnergyEngine,
        flags: EnergyFlags,
    ) -> None:
        self._hass = hass
        self._engine = engine
        self._flags = flags

    @staticmethod
    def _config_key(state: Any) -> str:
        """The automation's config id, else its entity object id."""
        attributes = dict(getattr(state, "attributes", {}) or {})
        value = attributes.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return str(state.entity_id).partition(".")[2]

    async def async_enforce_active(
        self, *, still_wanted: Callable[[], bool] | None = None
    ) -> None:
        """Turn off every enabled automation that is not flagged.

        The remembered set is saved after each turn_off, so a crash cannot
        lose what is owed a restore. still_wanted is asked before each
        turn_off; once it says no, the pass stops.
        """
        remembered = set(
            await self._hass.async_add_executor_job(self._flags.disabled_automations)
        )
        for state in sorted(
            (
                item
                for item in self._hass.states.async_all()
                if str(item.entity_id).startswith("automation.")
            ),
            key=lambda item: item.entity_id,
        ):
            if str(state.state) != "on" or state.entity_id in remembered:
                continue
            config_key = self._config_key(state)
            try:
                allowed = await self._hass.async_add_executor_job(
                    self._flags.works_during_energy_saving, config_key
                )
            except ValueError as err:
                # HA allows a longer id than a flag key; leave it running.
                _LOGGER.warning(
                    "Energy Saving skips automation %s: %s", state.entity_id, err
                )
                continue
            if allowed:
                continue
            if still_wanted is not None and not still_wanted():
                return
            try:
                await self._hass.services.async_call(
                    "automation",
                    "turn_off",
                    {"entity_id": state.entity_id},
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not disable automation %s for Energy Saving: %s",
                    state.entity_id,
                    err,
                )
                await self._record(
                    EVENT_AUTOMATION_DISABLE_FAILED,
                    state.entity_id,
                    {"config_key": config_key, "error": str(err)[:256]},
                    level=self._engine.active_level,
                )
                continue
            remembered.add(state.entity_id)
            await self._hass.async_add_executor_job(
                self._flags.set_disabled_automations, sorted(remembered)
            )
            await self._record(
                EVENT_AUTOMATION_DISABLED,
                state.entity_id,
                {"config_key": config_key},
                level=self._engine.active_level,
            )

    async def async_restore(self, *, level: str | None = None) -> None:
        """Switch back on what the hub switched off; failures stay owed."""
        pending = set(
            await self._hass.async_add_executor_job(self._flags.disabled_automations)
        )
        for entity_id in sorted(pending):
            try:
                await self._hass.services.async_call(
                    "automation",
                    "turn_on",
                    {"entity_id": entity_id},
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not restore automation %s after Energy Saving: %s",
                    entity_id,
                    err,
                )
                await self._record(
                    EVENT_AUTOMATION_RESTORE_FAILED,
                    entity_id,
                    {"error": str(err)[:256]},
                    level=level,
                )
                continue
            pending.discard(entity_id)
            await self._hass.async_add_executor_job(
                self._flags.set_disabled_automations, sorted(pending)
            )
            await self._record(EVENT_AUTOMATION_RESTORED, entity_id, {}, level=level)

    async def _record(
        self,
        kind: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        level: str | None,
    ) -> None:
        """Append an audit event; a failure is logged, never raised."""
        try:
            await self._hass.async_add_executor_job(
                lambda: self._engine.record_event(
                    kind,
                    level=level,
                    entity_id=entity_id,
                    data=data,
                )
            )
        except Exception:
            _LOGGER.exception("Could not record Energy Saving event %s", kind)


class EnergyController:
    """The entry point for start-up, the REST endpoints and factory reset."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: EnergyEngine,
        adapter: EnergyAdapter,
        automations: EnergyAutomationManager,
    ) -> None:
        self._hass = hass
        self.engine = engine
        self.adapter = adapter
        self.automations = automations
        # Startup/recovery, activate, deactivate and reapply run one at a time.
        # Each deactivate request bumps the generation, so an older transition
        # still waiting for the lock or part-way through can tell it lost.
        self._lock = asyncio.Lock()
        self._generation = 0
        self._apply_task: asyncio.Task[dict[str, Any]] | None = None
        self._startup: asyncio.Task[None] | None = None

    def notify_changed(self) -> None:
        """Tell WebSocket clients, the sensor and suggestions to re-read state."""
        self._hass.bus.async_fire(EVENT_ENERGY_CHANGED)

    async def async_start(self) -> None:
        """Start listening, and re-apply a level that was active at shutdown.

        The re-apply runs in the background: one slow device must not keep
        the hub in setup.
        """
        self.adapter.async_start()
        self._startup = self._hass.async_create_background_task(
            self._async_resume(self._generation), name="casasmart_energy_startup"
        )

    async def _async_resume(self, generation: int) -> None:
        """Re-apply the active level, or retry a failed automation restore."""
        async with self._lock:
            if generation != self._generation:
                return
            if self.engine.active_level is None:
                # A deactivation may have left a failed automation restore.
                await self.automations.async_restore()
                return
            if await self._async_apply("startup", generation) is None:
                return
            self.notify_changed()

    async def async_stop(self) -> None:
        """Stop listeners and timers once any transition in flight has ended.

        The device pass is cancelled, as a deactivate does. The active level
        is kept, so the next start applies it again.
        """
        self._generation += 1
        if self._apply_task is not None:
            self._apply_task.cancel()
        async with self._lock:
            self.adapter.async_stop()

    async def async_state(self) -> dict[str, Any]:
        """The engine snapshot plus the adapter's current issues and stats."""
        state, stats = await self._hass.async_add_executor_job(
            lambda: (self.engine.snapshot(), self.engine.stats())
        )
        return {**state, "issues": self.adapter.issues(), "stats": stats}

    async def async_activate(
        self,
        level: str,
        *,
        smart_lockout_enabled: bool | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Record the level as active, then apply it.

        Raises EnergyInactiveError when a deactivate arrives first.
        """
        generation = self._generation
        async with self._lock:
            self._raise_if_superseded(generation)
            state = await self._hass.async_add_executor_job(
                lambda: self.engine.activate(
                    level,
                    smart_lockout_enabled=smart_lockout_enabled,
                    actor=actor,
                )
            )
            applied = await self._async_apply("activation", generation)
            self._raise_if_superseded(generation)
            self.notify_changed()
            return {"state": state, "apply": applied}

    async def async_deactivate(self, *, actor: str | None = None) -> dict[str, Any]:
        """Stop the active level and switch its automations back on.

        Devices are left as they are.
        """
        # The newest request wins. Stop an apply in flight rather than wait
        # for every device in the home: the tablet gives up after 15 seconds.
        self._generation += 1
        if self._apply_task is not None:
            self._apply_task.cancel()
        async with self._lock:
            level = self.engine.active_level
            state = await self._hass.async_add_executor_job(
                lambda: self.engine.deactivate(actor=actor)
            )
            self.adapter.async_mode_stopped()
            await self.automations.async_restore(level=level)
            self.notify_changed()
            return state

    async def async_reapply(self, *, actor: str | None = None) -> dict[str, Any]:
        """Clear every release and apply the active level again."""
        generation = self._generation
        async with self._lock:
            self._raise_if_superseded(generation)
            state = await self._hass.async_add_executor_job(
                lambda: self.engine.reapply(actor=actor)
            )
            applied = await self._async_apply("reapply", generation)
            self._raise_if_superseded(generation)
            self.notify_changed()
            return {"state": state, "apply": applied}

    def _raise_if_superseded(self, generation: int) -> None:
        if generation != self._generation:
            raise EnergyInactiveError("Energy Saving was deactivated meanwhile")

    async def _async_apply(self, reason: str, generation: int) -> dict[str, Any] | None:
        """Switch automations off, then apply devices; None once superseded.

        The automation pass stops only between turn_off calls, so each one is
        remembered for the restore. The device pass is a task that a
        deactivate cancels, so a hung device cannot hold the deactivate up
        and no command from the abandoned pass lands after it.
        """

        def still_wanted() -> bool:
            return generation == self._generation

        await self.automations.async_enforce_active(still_wanted=still_wanted)
        if not still_wanted():
            return None
        task = asyncio.create_task(self.adapter.async_apply(reason=reason))
        self._apply_task = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return None
        finally:
            self._apply_task = None
