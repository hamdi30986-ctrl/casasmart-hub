"""CasaSmart Hub integration (Track B — B1.3: storage + REST skeleton).

Setup opens the B1.1 storage layer (SQLite+WAL + JSON config store) under
<ha-config>/casasmart/, parks it in runtime data, and registers the B1.3
REST views (version handshake + health probe). Entities and the entity
bridge arrive in B1.4+. Unload closes storage cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.loader import async_get_integration

from homeassistant.components import persistent_notification

from .api import async_register_views
from .auth_api import notify_recovery_code
from .auth_engine import AuthEngine
from .const import (
    BACKUP_DIR_NAME,
    DATA_DIR_NAME,
    DB_FILENAME,
    DOMAIN,
    HUB_CONFIG_FILENAME,
)
from .pairing import PairingManager
from .recovery import RecoveryManager
from .storage import HubStorage, JsonConfigStore, StorageError

_LOGGER = logging.getLogger(__name__)

type CasaSmartConfigEntry = ConfigEntry[CasaSmartRuntimeData]


@dataclass
class CasaSmartRuntimeData:
    """Objects the integration keeps alive for the lifetime of the entry."""

    storage: HubStorage
    hub_config: JsonConfigStore
    auth: AuthEngine
    pairing: PairingManager
    recovery: RecoveryManager


def _open_storage(
    data_dir: Path,
) -> tuple[
    HubStorage,
    JsonConfigStore,
    AuthEngine,
    PairingManager,
    RecoveryManager,
    str | None,
    str | None,
]:
    """Open storage + config + auth + pairing + recovery (blocking — executor only)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = HubStorage(
        db_path=data_dir / DB_FILENAME,
        backup_dir=data_dir / BACKUP_DIR_NAME,
    )
    storage.open()
    hub_config = JsonConfigStore(data_dir / HUB_CONFIG_FILENAME)
    auth = AuthEngine(storage.table("auth_devices"), hub_config)
    auth.warm_up()  # secret loaded here so request-path validation is pure CPU
    pairing = PairingManager(storage.table("pairing_codes"), auth.has_admin)
    # Unclaimed hub -> mint the initial admin pairing code (plan B2/B3:
    # the install card's QR; redeemable only while no admin exists).
    bootstrap_code = pairing.ensure_bootstrap_code()
    recovery = RecoveryManager(storage.table("recovery_codes"), auth.has_admin)
    # Claimed hub without a recovery code (hub claimed before B3 shipped,
    # or the previous code was redeemed mid-crash) -> arm one now.
    recovery_code = recovery.ensure_armed()
    return storage, hub_config, auth, pairing, recovery, bootstrap_code, recovery_code


async def async_setup_entry(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> bool:
    """Set up CasaSmart Hub from a config entry."""
    data_dir = Path(hass.config.path(DATA_DIR_NAME))

    try:
        (
            storage,
            hub_config,
            auth,
            pairing,
            recovery,
            bootstrap_code,
            recovery_code,
        ) = await hass.async_add_executor_job(_open_storage, data_dir)
    except StorageError as err:
        raise ConfigEntryNotReady(f"CasaSmart storage failed to open: {err}") from err

    entry.runtime_data = CasaSmartRuntimeData(
        storage=storage,
        hub_config=hub_config,
        auth=auth,
        pairing=pairing,
        recovery=recovery,
    )

    if bootstrap_code is not None:
        # Surfaced once, to the HA admin only — this is how the owner's
        # phone claims an unclaimed hub (B2 bootstrap; card QR later).
        persistent_notification.async_create(
            hass,
            f"Initial admin pairing code: **{bootstrap_code}**\n\n"
            "Use it in the CasaSmart app (on this network) to claim the "
            "hub. It stays valid until an admin is paired.",
            title="CasaSmart Hub — pairing code",
            notification_id=f"{DOMAIN}_bootstrap_pairing",
        )

    if recovery_code is not None:
        # Plaintext exists exactly once — Hamdi engraves the metal card
        # from this notification (B3 backup tier).
        notify_recovery_code(hass, recovery_code)

    _async_register_services(hass)

    # Hub version = the integration's manifest version (single source of truth).
    integration = await async_get_integration(hass, DOMAIN)
    hub_version = str(integration.version) if integration.version else "0.0.0"
    async_register_views(hass, hub_version=hub_version)

    _LOGGER.info("CasaSmart Hub storage ready at %s", data_dir)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register hub services (idempotent across entry reloads).

    ``casasmart.factory_reset`` is the B3 nuclear tier: Hamdi, over
    Tailscale -> HA or on-site, wipes the CasaSmart APP layer only —
    paired phones, pairing codes, recovery code. HA devices, automations
    and the Zigbee mesh are untouched. Reachable only through HA itself
    (admin login or trusted CLI), never through the CasaSmart API — a
    stolen app token can't factory-reset the hub.
    """
    if hass.services.has_service(DOMAIN, "factory_reset"):
        return

    async def _handle_factory_reset(call) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("CasaSmart hub is not loaded")
        runtime_data: CasaSmartRuntimeData = entries[0].runtime_data

        def _wipe() -> None:
            runtime_data.storage.table("auth_devices").clear()
            runtime_data.storage.table("pairing_codes").clear()
            runtime_data.storage.table("recovery_codes").clear()

        await hass.async_add_executor_job(_wipe)
        _LOGGER.warning(
            "CasaSmart factory reset: app layer wiped (devices, pairing, recovery)"
        )
        # Reload rebuilds the engine caches from the now-empty tables and
        # re-mints the bootstrap pairing code for re-onboarding.
        await hass.config_entries.async_reload(entries[0].entry_id)

    hass.services.async_register(DOMAIN, "factory_reset", _handle_factory_reset)


async def async_unload_entry(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> bool:
    """Unload a config entry, closing storage cleanly."""
    await hass.async_add_executor_job(entry.runtime_data.storage.close)
    _LOGGER.info("CasaSmart Hub storage closed")
    return True
