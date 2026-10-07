"""Owner buttons: regenerate the pairing code, and factory reset.

Both are reachable only through Home Assistant, which is the owner's
authorization boundary; an app token can't press them.

button.casasmart_regenerate_pairing_code resets pairing. It unpairs every
device (their tokens stop working at once), deletes every pairing code,
clears push tokens, favorites and per-user settings, and mints a new admin
bootstrap code. The code is shown once, in a persistent notification, and
its hash replaces the printed sticker's, so the old sticker stops working.
Sub-admin and user codes come only from the app's family-share screen.

button.casasmart_factory_reset calls the casasmart.factory_reset service.
"""

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
    """Register the hub's owner-control buttons (regenerate pairing + factory reset)."""
    async_add_entities(
        [
            CasaSmartRegeneratePairingButton(hass, entry),
            CasaSmartFactoryResetButton(hass, entry),
        ]
    )


class CasaSmartRegeneratePairingButton(ButtonEntity):
    """Resets pairing and mints a new admin code."""

    _attr_has_entity_name = True
    _attr_name = "Regenerate pairing code"
    _attr_icon = "mdi:key-change"

    def __init__(self, hass: HomeAssistant, entry: CasaSmartConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_regenerate_pairing_code"
        # The app expects this exact id; without it has_entity_name would
        # prefix the device name. Only applies on first registration.
        self.entity_id = ENTITY_ID_FORMAT.format("casasmart_regenerate_pairing_code")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    async def async_press(self) -> None:
        """Unpair every device, delete all codes and mint a new admin code."""
        data = self._entry.runtime_data
        auth = data.auth
        pairing = data.pairing

        def _regenerate() -> dict:
            wiped_devices = auth.wipe_all_devices()
            wiped_codes = pairing.clear_all_codes()
            # Stop pushes to the unpaired phones.
            data.storage.table("push_tokens").clear()
            # Favorites and settings belong to the unpaired members.
            data.storage.table("registry_favorites").clear()
            data.storage.table("user_settings").clear()
            # With no admin left, this mints an admin code and returns it.
            code = pairing.ensure_bootstrap_code()
            # Store the hash as the new sticker code, so it survives restarts.
            # The printed sticker stops working.
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
            # Can't happen right after a wipe, but don't claim a code we
            # don't have.
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
        # Updates the per-device sensors.
        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})
        _LOGGER.info(
            "Pairing factory reset: unpaired %d device(s), wiped %d code(s), "
            "admin bootstrap code re-minted: %s",
            device_count,
            code_count,
            "yes" if code is not None else "no",
        )


class CasaSmartFactoryResetButton(ButtonEntity):
    """Factory reset, for last-resort recovery or a change of owner.

    Calls casasmart.factory_reset, which wipes the app layer (the tables in
    const.FACTORY_RESET_TABLES) and issues new admin and recovery codes, so
    the printed sticker and recovery card stop working. Tanks, alarm zones
    and settings, everything in Home Assistant, and the hub's TLS, push,
    relay and tunnel setup are kept.
    """

    _attr_has_entity_name = True
    _attr_name = "Factory reset"
    _attr_icon = "mdi:alert-octagon"

    def __init__(self, hass: HomeAssistant, entry: CasaSmartConfigEntry) -> None:
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
        """Start casasmart.factory_reset without waiting for it.

        The service reloads this config entry, so waiting would mean waiting
        on this platform's own unload.
        """
        _LOGGER.warning("CasaSmart factory reset requested via button")
        persistent_notification.async_create(
            self._hass,
            "Factory reset triggered — the app layer (paired phones, codes, "
            "favorites, scenes, settings, push tokens, alarm log/state) is being "
            "wiped and the previous owner's device labels scrubbed. Rooms re-seed "
            "from Home Assistant; tanks, alarm zones and everything in Home "
            "Assistant are kept. FRESH admin + recovery codes will be posted here "
            "after the reset — the OLD printed sticker and metal card are now "
            "dead; re-sticker the hub with the new code.",
            title="CasaSmart Hub — factory reset",
            notification_id=f"{DOMAIN}_factory_reset",
        )
        await self._hass.services.async_call(DOMAIN, "factory_reset", blocking=False)
