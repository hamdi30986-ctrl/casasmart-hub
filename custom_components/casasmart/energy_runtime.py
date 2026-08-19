"""CasaSmart runtime component."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .auth_tokens import ROLE_ADMIN
from .const import EVENT_ENERGY_CHANGED
from .energy import EnergyEngine
from .energy_adapter import EnergyAdapter

_LOGGER = logging.getLogger(__name__)

_FLAG_PREFIX = "automation:"
_DISABLED_KEY = "_disabled_automations"

EVENT_AUTOMATION_DISABLED = "automation_disabled"
EVENT_AUTOMATION_RESTORED = "automation_restored"
EVENT_AUTOMATION_DISABLE_FAILED = "automation_disable_failed"
EVENT_AUTOMATION_RESTORE_FAILED = "automation_restore_failed"


class EnergyFlags:
    """CasaSmart runtime component."""

    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _clean_key(config_key: Any) -> str:
        if not isinstance(config_key, str) or not config_key.strip():
            raise ValueError("automation config key must be a non-empty string")
        key = config_key.strip()
        if len(key) > 255:
            raise ValueError("automation config key must be <= 255 characters")
        return key

    def works_during_energy_saving(self, config_key: str) -> bool:
        key = self._clean_key(config_key)
        value = self._table.get(f"{_FLAG_PREFIX}{key}")
        return bool(
            isinstance(value, dict)
            and value.get("works_during_energy_saving") is True
        )

    def set_works_during_energy_saving(
        self, config_key: str, enabled: Any
    ) -> bool:
        key = self._clean_key(config_key)
        if not isinstance(enabled, bool):
            raise ValueError("works_during_energy_saving must be a boolean")
        self._table[f"{_FLAG_PREFIX}{key}"] = {
            "works_during_energy_saving": enabled
        }
        return enabled

    def delete_automation(self, config_key: str) -> None:
        key = self._clean_key(config_key)
        self._table.pop(f"{_FLAG_PREFIX}{key}", None)

    def disabled_automations(self) -> list[str]:
        value = self._table.get(_DISABLED_KEY)
        if not isinstance(value, dict) or not isinstance(
            value.get("entity_ids"), list
        ):
            return []
        return sorted(
            {
                item.strip()
                for item in value["entity_ids"]
                if isinstance(item, str)
                and item.strip().startswith("automation.")
            }
        )

    def set_disabled_automations(self, entity_ids: list[str]) -> None:
        clean = sorted(
            {
                item.strip()
                for item in entity_ids
                if isinstance(item, str)
                and item.strip().startswith("automation.")
            }
        )
        if clean:
            self._table[_DISABLED_KEY] = {"entity_ids": clean}
        else:
            self._table.pop(_DISABLED_KEY, None)

    def clear(self) -> None:
        self._table.clear()


def energy_lockout_applies(engine: EnergyEngine, claims: dict[str, Any]) -> bool:
    """CasaSmart runtime component."""
    state = engine.snapshot()
    return bool(
        state["active"]
        and state["lockout_enabled"]
        and claims.get("role") != ROLE_ADMIN
    )


class EnergyAutomationManager:
    """CasaSmart runtime component."""

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
        attributes = dict(getattr(state, "attributes", {}) or {})
        value = attributes.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return str(state.entity_id).partition(".")[2]

    async def async_enforce_active(self) -> None:
        """CasaSmart runtime component."""
        remembered = set(
            await self._hass.async_add_executor_job(
                self._flags.disabled_automations
            )
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
            allowed = await self._hass.async_add_executor_job(
                self._flags.works_during_energy_saving, config_key
            )
            if allowed:
                continue
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
        """CasaSmart runtime component."""
        pending = set(
            await self._hass.async_add_executor_job(
                self._flags.disabled_automations
            )
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
            await self._record(
                EVENT_AUTOMATION_RESTORED, entity_id, {}, level=level
            )

    async def _record(
        self,
        kind: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        level: str | None,
    ) -> None:
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
    """CasaSmart runtime component."""

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

    def notify_changed(self) -> None:
        self._hass.bus.async_fire(EVENT_ENERGY_CHANGED)

    async def async_start(self) -> None:
        self.adapter.async_start()
        if self.engine.active_level is None:


            await self.automations.async_restore()
            return
        await self.automations.async_enforce_active()
        await self.adapter.async_apply(reason="startup")
        self.notify_changed()

    def async_stop(self) -> None:
        """CasaSmart runtime component."""
        self.adapter.async_stop()

    async def async_state(self) -> dict[str, Any]:
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
        state = await self._hass.async_add_executor_job(
            lambda: self.engine.activate(
                level,
                smart_lockout_enabled=smart_lockout_enabled,
                actor=actor,
            )
        )
        await self.automations.async_enforce_active()
        applied = await self.adapter.async_apply(reason="activation")
        self.notify_changed()
        return {"state": state, "apply": applied}

    async def async_deactivate(self, *, actor: str | None = None) -> dict[str, Any]:
        level = self.engine.active_level
        state = await self._hass.async_add_executor_job(
            lambda: self.engine.deactivate(actor=actor)
        )
        self.adapter.async_mode_stopped()
        await self.automations.async_restore(level=level)
        self.notify_changed()
        return state

    async def async_reapply(self, *, actor: str | None = None) -> dict[str, Any]:
        state = await self._hass.async_add_executor_job(
            lambda: self.engine.reapply(actor=actor)
        )
        await self.automations.async_enforce_active()
        applied = await self.adapter.async_apply(reason="reapply")
        self.notify_changed()
        return {"state": state, "apply": applied}
