"""Find the loaded CasaSmart config entry from code that only has hass.

Views and service handlers look the entry up on every call instead of keeping
it, so they follow reloads and see None while the hub isn't loaded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from . import CasaSmartRuntimeData


def loaded_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The loaded CasaSmart config entry, or None when not set up."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def loaded_runtime_data(hass: HomeAssistant) -> CasaSmartRuntimeData | None:
    """The loaded entry's runtime data, or None when not set up."""
    entry = loaded_entry(hass)
    return entry.runtime_data if entry is not None else None
