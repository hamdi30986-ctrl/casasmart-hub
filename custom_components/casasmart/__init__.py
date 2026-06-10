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
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.loader import async_get_integration

from homeassistant.components import persistent_notification

from .api import async_register_views
from .auth_engine import AuthEngine
from .const import (
    BACKUP_DIR_NAME,
    DATA_DIR_NAME,
    DB_FILENAME,
    DOMAIN,
    HUB_CONFIG_FILENAME,
)
from .pairing import PairingManager
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


def _open_storage(
    data_dir: Path,
) -> tuple[HubStorage, JsonConfigStore, AuthEngine, PairingManager, str | None]:
    """Open storage + config + auth + pairing (blocking — executor only)."""
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
    return storage, hub_config, auth, pairing, bootstrap_code


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
            bootstrap_code,
        ) = await hass.async_add_executor_job(_open_storage, data_dir)
    except StorageError as err:
        raise ConfigEntryNotReady(f"CasaSmart storage failed to open: {err}") from err

    entry.runtime_data = CasaSmartRuntimeData(
        storage=storage, hub_config=hub_config, auth=auth, pairing=pairing
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

    # Hub version = the integration's manifest version (single source of truth).
    integration = await async_get_integration(hass, DOMAIN)
    hub_version = str(integration.version) if integration.version else "0.0.0"
    async_register_views(hass, hub_version=hub_version)

    _LOGGER.info("CasaSmart Hub storage ready at %s", data_dir)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> bool:
    """Unload a config entry, closing storage cleanly."""
    await hass.async_add_executor_job(entry.runtime_data.storage.close)
    _LOGGER.info("CasaSmart Hub storage closed")
    return True
