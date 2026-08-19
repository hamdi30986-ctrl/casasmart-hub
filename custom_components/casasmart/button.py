"""CasaSmart runtime component."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.components.button import ENTITY_ID_FORMAT, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BOOTSTRAP_CODE_HASH_CONFIG_KEY, DOMAIN, EVENT_AUTH_CHANGED
from .pairing import hash_code as pairing_hash_code

if TYPE_CHECKING:
    from . import CasaSmartConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """CasaSmart runtime component."""
    async_add_entities(
        [
            CasaSmartRegeneratePairingButton(hass, entry),
            CasaSmartFactoryResetButton(hass, entry),
        ]
    )


class CasaSmartRegeneratePairingButton(ButtonEntity):
    """CasaSmart runtime component."""

    _attr_has_entity_name = False
    _attr_name = "CasaSmart Regenerate Pairing Code"
    _attr_icon = "mdi:key-change"

    def __init__(
        self, hass: HomeAssistant, entry: CasaSmartConfigEntry
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_regenerate_pairing_code"




        self.entity_id = ENTITY_ID_FORMAT.format("casasmart_regenerate_pairing_code")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    async def async_press(self) -> None:
        """CasaSmart runtime component."""
        data = self._entry.runtime_data
        auth = data.auth
        pairing = data.pairing

        def _regenerate() -> dict:
            wiped_devices = auth.wipe_all_devices()
            wiped_codes = pairing.clear_all_codes()



            data.storage.table("push_tokens").clear()


            data.storage.table("registry_favorites").clear()
            data.storage.table("user_settings").clear()


            code = pairing.ensure_bootstrap_code()



            if code is not None:
                data.hub_config.set(
                    BOOTSTRAP_CODE_HASH_CONFIG_KEY, pairing_hash_code(code)
                )
            return {
                "code": code,
                "wiped_devices": wiped_devices,
                "wiped_codes": wiped_codes,
            }

        result = await self._hass.async_add_executor_job(_regenerate)
        code = result["code"]
        device_count = len(result["wiped_devices"])
        code_count = result["wiped_codes"]

        if code is not None:
            body = (
                f"New owner pairing code: **{code}**\n\n"
                "Role: admin · never expires · LAN-only · valid while unclaimed.\n"
                "⚠️ This ROTATES the permanent code — the OLD printed sticker is "
                "now dead. Re-sticker the hub with this new code.\n\n"
                f"Pairing was reset: {device_count} device(s) unpaired, "
                f"{code_count} code(s) cleared.\n\n"
                "Pair the owner's phone in the CasaSmart app on this network. "
                "Add family members later from the app's family-share screen."
            )
        else:



            body = (
                f"Pairing was reset: {device_count} device(s) unpaired, "
                f"{code_count} code(s) cleared.\n\n"
                "No new code was minted — re-run the reset or check the logs."
            )

        persistent_notification.async_create(
            self._hass,
            body,
            title="CasaSmart Hub — pairing reset",
            notification_id=f"{DOMAIN}_regenerated_pairing",
        )

        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})
        _LOGGER.info(
            "Pairing factory reset: unpaired %d device(s), wiped %d code(s), "
            "admin bootstrap code re-minted: %s",
            device_count,
            code_count,
            "yes" if code is not None else "no",
        )


class CasaSmartFactoryResetButton(ButtonEntity):
    """CasaSmart runtime component."""

    _attr_has_entity_name = False
    _attr_name = "CasaSmart Factory Reset"
    _attr_icon = "mdi:alert-octagon"

    def __init__(
        self, hass: HomeAssistant, entry: CasaSmartConfigEntry
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_factory_reset"
        self.entity_id = ENTITY_ID_FORMAT.format("casasmart_factory_reset")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    async def async_press(self) -> None:
        """CasaSmart runtime component."""
        _LOGGER.warning("CasaSmart factory reset requested via button")
        persistent_notification.async_create(
            self._hass,
            "Factory reset triggered — the app layer (paired phones, codes, "
            "favorites, settings, push tokens, alarm log/state) is being wiped "
            "and the previous owner's device labels scrubbed. House data (rooms, "
            "scenes, tanks) and device wiring are kept. FRESH admin + recovery "
            "codes will be posted here after the reset — the OLD printed sticker "
            "and metal card are now dead; re-sticker the hub with the new code.",
            title="CasaSmart Hub — factory reset",
            notification_id=f"{DOMAIN}_factory_reset",
        )
        await self._hass.services.async_call(DOMAIN, "factory_reset", blocking=False)
