"""CasaSmart 'Regenerate pairing code' button — pairing factory reset.

One button entity — ``button.casasmart_regenerate_pairing_code`` — the hub
owner presses to FACTORY-RESET pairing. A single press, in one executor job:

1. Unpairs EVERY enrolled device — admin, sub-admins and users alike. Their
   JWTs die instantly (the same ``ver`` kill as an unpair), the login-throttle
   counters reset, and the hub is handed back to the unclaimed state.
2. Wipes every outstanding pairing code, the bootstrap admin code included —
   afterwards no code exists at all.
3. Mints a FRESH **admin** bootstrap code. With no admin left after step 1,
   ``ensure_bootstrap_code`` re-arms the unclaimed-hub onboarding path, so the
   owner re-pairs from scratch — the hub only ever mints admin codes.
4. Surfaces the new plaintext admin code as an HA persistent notification —
   the one and only time it exists in the clear.

Sub-admin and user access are NOT minted here: they come exclusively from the
app's family-share screen, which POSTs to ``/api/casasmart/pairing/codes``
behind the admin-only ``pairing.generate`` gate. No hub button is involved.

Like ``casasmart.factory_reset``, the control is reachable only through Home
Assistant itself: pressing it requires HA access (the owner over Tailscale or
on-site), which IS the hub's owner-authorization boundary — a stolen app token
can never reach it.
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
    """The owner's one-press 'factory-reset pairing' control."""

    _attr_has_entity_name = False
    _attr_name = "CasaSmart Regenerate Pairing Code"
    _attr_icon = "mdi:key-change"

    def __init__(
        self, hass: HomeAssistant, entry: CasaSmartConfigEntry
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_regenerate_pairing_code"
        # Pin the exact entity id the plan/app expect, independent of the hub
        # device name (a device-named entity would become
        # ``button.casasmart_hub_…``). Set on first registration only; the
        # entity still groups under the hub device below.
        self.entity_id = ENTITY_ID_FORMAT.format("casasmart_regenerate_pairing_code")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="CasaSmart Hub",
            manufacturer="CasaSmart",
        )

    async def async_press(self) -> None:
        """Wipe every device + code, then mint a fresh admin bootstrap code."""
        data = self._entry.runtime_data
        auth = data.auth
        pairing = data.pairing

        def _regenerate() -> dict:
            wiped_devices = auth.wipe_all_devices()
            wiped_codes = pairing.clear_all_codes()
            # No admin remains after the wipe, so this mints a fresh ADMIN
            # bootstrap code and returns its plaintext.
            code = pairing.ensure_bootstrap_code()
            # ROTATE the permanent sticker code: persist the new hash so it
            # survives factory reset and is the one the boot path re-installs.
            # The OLD printed sticker is now dead — the hub must be re-stickered.
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
            # ensure_bootstrap_code only returns None if an admin still exists
            # or a code is already outstanding — neither is reachable right
            # after a wipe, but never claim a code we don't actually hold.
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
        # The enrolled set was wiped — refresh the per-user sensors.
        self._hass.bus.async_fire(EVENT_AUTH_CHANGED, {})
        _LOGGER.info(
            "Pairing factory reset: unpaired %d device(s), wiped %d code(s), "
            "admin bootstrap code re-minted: %s",
            device_count,
            code_count,
            "yes" if code is not None else "no",
        )


class CasaSmartFactoryResetButton(ButtonEntity):
    """Nuclear reset (B3 tier 3) — wipes the CasaSmart APP layer only.

    Unpaired phones, pairing codes, recovery codes, favorites, per-user settings
    and push tokens are cleared; HOUSE data (rooms, scenes, tanks, the HA
    devices/automations/Zigbee mesh) survives. The PERMANENT codes survive too —
    their hashes live in hub_config, so after the reset the SAME printed sticker
    and metal card re-onboard the owner. Operator-only: reachable through Home
    Assistant (admin login / on-site / Tailscale), never the CasaSmart API.
    """

    _attr_has_entity_name = False
    _attr_name = "CasaSmart Factory Reset"
    _attr_icon = "mdi:alert-octagon"  # danger: wipes the app layer

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
        """Trigger casasmart.factory_reset (wipe app layer + reload).

        Non-blocking: the service reloads this very config entry, so awaiting it
        would wait on our own platform unloading. The permanent sticker + metal
        card still re-onboard the owner after the reset.
        """
        _LOGGER.warning("CasaSmart factory reset requested via button")
        persistent_notification.async_create(
            self._hass,
            "Factory reset triggered — the CasaSmart app layer (paired phones, "
            "codes, favorites, settings, push tokens) is being wiped. House data "
            "(rooms, scenes, tanks) is kept. Re-onboard the owner with the "
            "**printed sticker** code on this network; the metal recovery card "
            "still works too.",
            title="CasaSmart Hub — factory reset",
            notification_id=f"{DOMAIN}_factory_reset",
        )
        await self._hass.services.async_call(DOMAIN, "factory_reset", blocking=False)
