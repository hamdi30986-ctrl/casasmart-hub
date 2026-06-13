"""CasaSmart Hub integration (Track B — B1.3: storage + REST skeleton).

Setup opens the B1.1 storage layer (SQLite+WAL + JSON config store) under
<ha-config>/casasmart/, parks it in runtime data, and registers the B1.3
REST views (version handshake + health probe). Entities and the entity
bridge arrive in B1.4+. B10 adds the hub's permanent TLS identity and the
dedicated HTTPS listener. Unload stops the listener and closes storage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.loader import async_get_integration

from homeassistant.components import persistent_notification

from .api import async_register_views, build_views
from .auth_api import notify_recovery_code
from .auth_engine import AuthEngine
from .discovery import MdnsAdvertiser
from .const import (
    API_VERSION,
    BACKUP_DIR_NAME,
    DATA_DIR_NAME,
    DB_FILENAME,
    DOMAIN,
    HUB_CONFIG_FILENAME,
    HUB_NAME_CONFIG_KEY,
    MDNS_REFRESH_INTERVAL_MINUTES,
    TLS_CERT_CHECK_INTERVAL_HOURS,
    TLS_PORT_DEFAULT,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

from .entity_bridge import is_exposed
from .pairing import PairingManager
from .alarm import AlarmEngine
from .alarm_adapter import AlarmAdapter
from .recovery import RecoveryManager
from .registry import RegistryEngine
from .storage import HubStorage, JsonConfigStore, StorageError
from .tank import TankEngine
from .tls import CasaSmartTlsServer, IdentityError, ensure_tls_material
from .user_settings import UserSettingsEngine

_LOGGER = logging.getLogger(__name__)

# B13: the hub's only HA entity platform — the alarm panel mirroring the
# hub-authoritative AlarmEngine. Everything else is REST/WS, not HA entities.
PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]

type CasaSmartConfigEntry = ConfigEntry[CasaSmartRuntimeData]


@dataclass
class CasaSmartRuntimeData:
    """Objects the integration keeps alive for the lifetime of the entry."""

    storage: HubStorage
    hub_config: JsonConfigStore
    auth: AuthEngine
    pairing: PairingManager
    recovery: RecoveryManager
    registry: RegistryEngine
    tanks: TankEngine
    user_settings: UserSettingsEngine
    # B13 hub-side alarm state machine (push leg stubbed until B8).
    alarm: AlarmEngine
    # B13 alarm HA glue: drives the engine off state_changed + the entry-delay
    # timer. None only if setup failed before it was started.
    alarm_adapter: AlarmAdapter | None = None
    # B10 HTTPS listener; None only if the identity/cert layer failed setup.
    tls: CasaSmartTlsServer | None = None
    # B6 mDNS advertiser; None if TLS identity (its hub-id source) is absent
    # or zeroconf wasn't ready — discovery degrades, the hub stays reachable.
    mdns: MdnsAdvertiser | None = None


def _open_storage(
    data_dir: Path,
) -> tuple[
    HubStorage,
    JsonConfigStore,
    AuthEngine,
    PairingManager,
    RecoveryManager,
    RegistryEngine,
    TankEngine,
    UserSettingsEngine,
    AlarmEngine,
    str | None,
    str | None,
]:
    """Open storage + config + auth + pairing + recovery + registry +
    tanks + user settings (blocking — executor only)."""
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
    registry = RegistryEngine(
        storage.table("registry_floors"),
        storage.table("registry_rooms"),
        storage.table("registry_devices"),
        storage.table("registry_scenes"),
        storage.table("registry_favorites"),
    )
    registry.warm_up()  # room/name mirrors loaded — event-loop reads stay pure CPU
    tanks = TankEngine(
        storage.table("tank_devices"),
        storage.table("tank_readings"),
    )
    user_settings = UserSettingsEngine(storage.table("user_settings"))
    # B13 alarm: persisted arm state + zone map + bounded event history. The
    # push leg (alert_sink) is left at its no-op default until B8 wires the
    # relay; the HA adapter that drives process_sensor/tick is a later piece.
    alarm = AlarmEngine(
        storage.table("alarm_state"),
        storage.table("alarm_zones"),
        storage.table("alarm_history"),
        storage.table("alarm_settings"),
    )
    alarm.warm_up()  # arm state + zones loaded — event-loop reads stay pure CPU
    return (
        storage,
        hub_config,
        auth,
        pairing,
        recovery,
        registry,
        tanks,
        user_settings,
        alarm,
        bootstrap_code,
        recovery_code,
    )


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
            registry,
            tanks,
            user_settings,
            alarm,
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
        registry=registry,
        tanks=tanks,
        user_settings=user_settings,
        alarm=alarm,
    )

    await _async_import_registry(hass, hub_config, registry)

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

    await _async_start_tls(hass, entry, data_dir, hub_version)
    await _async_start_mdns(hass, entry)

    # B13: wire the alarm engine to live HA events (sensor edges + the
    # entry-delay timer). Pure event subscription — no blocking work.
    alarm_adapter = AlarmAdapter(hass, alarm)
    alarm_adapter.async_start()
    entry.runtime_data.alarm_adapter = alarm_adapter

    # B13: expose the alarm panel as a native HA entity (runtime_data — its
    # engine source — is already set above, so the platform can read it).
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("CasaSmart Hub storage ready at %s", data_dir)
    return True


async def _async_import_registry(
    hass: HomeAssistant,
    hub_config: JsonConfigStore,
    registry: RegistryEngine,
) -> None:
    """B17 first-run import: seed the registry from HA's own registries.

    Floors/areas/entity-area assignments become registry floors/rooms/
    assignments KEEPING their HA ids — existing room-scoped JWTs use HA
    area ids, so imported layouts work with them unchanged. Runs once
    (``registry_imported`` flag); the engine additionally never
    overwrites existing records, so a re-run after a crash mid-import
    completes the seed without clobbering installer edits.
    """
    if hub_config.get("registry_imported") is True:
        return

    floor_registry = fr.async_get(hass)
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)

    floors = [
        {
            "floor_id": floor.floor_id,
            "name": floor.name,
            "sort_order": floor.level or 0,
        }
        for floor in floor_registry.async_list_floors()
    ]
    rooms = [
        {
            "room_id": area.id,
            "name": area.name,
            "floor_id": area.floor_id,
            "icon": area.icon,
        }
        for area in area_registry.async_list_areas()
    ]
    device_registry = dr.async_get(hass)
    assignments = []
    for entry in entity_registry.entities.values():
        if not is_exposed(entry.entity_id):
            continue
        area_id = entry.area_id
        if area_id is None and entry.device_id is not None:
            device = device_registry.async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id is not None:
            assignments.append({"entity_id": entry.entity_id, "room_id": area_id})

    def _seed() -> dict[str, int]:
        counts = registry.import_initial(floors, rooms, assignments)
        hub_config.set("registry_imported", True)
        return counts

    try:
        counts = await hass.async_add_executor_job(_seed)
    except Exception:  # noqa: BLE001 — a failed seed must never kill setup
        # Flag stays unset -> retried next boot; import_initial never
        # overwrites, so a partial seed just gets completed then.
        _LOGGER.exception("Registry seed failed — will retry on next start")
        return
    _LOGGER.info(
        "Registry seeded from HA: %d floors, %d rooms, %d assignments",
        counts["floors"],
        counts["rooms"],
        counts["assignments"],
    )


async def _async_start_tls(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    data_dir: Path,
    hub_version: str,
) -> None:
    """B10: bring up the dedicated HTTPS listener + the daily cert check.

    A corrupt identity key aborts setup loudly (re-keying silently would
    break every paired phone's pin — tls.py documents the recovery). A
    port that won't bind does NOT abort: it's logged and retried on the
    daily tick, and the plain views on HA's port keep working meanwhile.
    """
    runtime_data = entry.runtime_data
    try:
        material = await hass.async_add_executor_job(ensure_tls_material, data_dir)
    except IdentityError as err:
        # Non-transient by definition (corrupt identity key) — retrying
        # can't fix it, a human must. ConfigEntryError, not NotReady.
        raise ConfigEntryError(str(err)) from err

    port = runtime_data.hub_config.get("tls_port")
    if not isinstance(port, int):
        port = TLS_PORT_DEFAULT

    server = CasaSmartTlsServer(hass, port, material)
    runtime_data.tls = server
    await server.async_start(build_views(hass, hub_version))

    async def _daily_cert_check(_now) -> None:
        try:
            fresh = await hass.async_add_executor_job(ensure_tls_material, data_dir)
        except IdentityError:
            _LOGGER.exception("Daily TLS check: identity key became unusable")
            return
        await server.async_refresh(fresh, build_views(hass, hub_version))

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _daily_cert_check,
            timedelta(hours=TLS_CERT_CHECK_INTERVAL_HOURS),
        )
    )


async def _async_start_mdns(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """B6: advertise ``_casasmart._tcp`` so the app auto-discovers the hub.

    The hub-id broadcast in the TXT record is the **permanent identity
    fingerprint** (B10) — the same value the app pins — so discovery and
    the TLS trust decision share one identity, and a spoofed TXT id can't
    survive the pin. Needs that fingerprint, so it runs after TLS; if the
    identity layer failed (``runtime_data.tls is None``) there's nothing
    stable to advertise and discovery is skipped (the stored-IP/tunnel
    chain still reaches the hub). Re-publishes on a slow tick to follow a
    DHCP IP change. Failures degrade silently — mDNS is convenience.
    """
    runtime_data = entry.runtime_data
    if runtime_data.tls is None:
        _LOGGER.info("mDNS advertiser skipped — TLS identity unavailable")
        return

    hub_name = runtime_data.hub_config.get(HUB_NAME_CONFIG_KEY)
    advertiser = MdnsAdvertiser(
        hass,
        hub_id=runtime_data.tls.material.identity_fingerprint,
        hub_name=hub_name if isinstance(hub_name, str) else None,
        api_version=API_VERSION,
        port=runtime_data.tls.port,
    )
    await advertiser.async_start()
    runtime_data.mdns = advertiser

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            advertiser.async_refresh,
            timedelta(minutes=MDNS_REFRESH_INTERVAL_MINUTES),
        )
    )


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
            # B17: favorites are phone/app-layer data — they die with the
            # phones. Floors/rooms/scenes are HOUSE data and survive.
            runtime_data.storage.table("registry_favorites").clear()
            # MB-2: per-user settings are phone-layer too, same fate.
            # Tank devices/readings are HOUSE data (the Shelly keeps
            # posting through an ownership transfer) and survive.
            runtime_data.storage.table("user_settings").clear()

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
    """Unload a config entry, stopping the mDNS/TLS listeners and storage."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if entry.runtime_data.alarm_adapter is not None:
        entry.runtime_data.alarm_adapter.async_stop()
    if entry.runtime_data.mdns is not None:
        await entry.runtime_data.mdns.async_stop()
    if entry.runtime_data.tls is not None:
        await entry.runtime_data.tls.async_stop()
    await hass.async_add_executor_job(entry.runtime_data.storage.close)
    _LOGGER.info("CasaSmart Hub storage closed")
    return True
