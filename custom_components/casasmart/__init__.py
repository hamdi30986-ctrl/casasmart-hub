"""CasaSmart runtime component."""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
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
    BOOTSTRAP_CODE_HASH_CONFIG_KEY,
    CONF_CLOUDFLARE_DOMAIN,
    CONF_PUSH_RELAY_URL,
    CONF_RELAY_ACTIVATION_CODE,
    CONF_RELAY_ACTIVATION_REQUEST_ID,
    CONF_TUNNEL_ENABLED,
    CONFIG_ENTRY_VERSION,
    DATA_DIR_NAME,
    DB_FILENAME,
    DOMAIN,
    EVENT_AUTH_CHANGED,
    EVENT_ENERGY_CHANGED,
    HUB_CONFIG_FILENAME,
    HUB_NAME_CONFIG_KEY,
    MDNS_REFRESH_INTERVAL_MINUTES,
    PUSH_RELAY_URL_CONFIG_KEY,
    PROVISION_SECRET_CONFIG_KEY,
    RECOVERY_CODE_HASH_CONFIG_KEY,
    TLS_CERT_CHECK_INTERVAL_HOURS,
    TLS_PORT_DEFAULT,
    TUNNEL_WATCHDOG_INTERVAL_MINUTES,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .dev_enroll import ensure_dev_devices
from .entity_bridge import is_exposed
from .pairing import PairingManager, hash_code as pairing_hash_code
from .push import PushTokenStore
from .push_crypto import PushIdentityError, ensure_push_identity
from .push_dispatcher import PushDispatcher, TankPushMonitor
from .relay_config import (
    RelayConfigSnapshot,
    async_reload_relay_runtime,
    migrate_relay_options,
    normalize_relay_base_url,
    relay_config_snapshot,
    relay_endpoints,
    relay_reload_required,
    without_relay_activation,
)
from .relay_registration import RelayRegistrar, is_activation_code_format
from .alarm import AlarmEngine
from .alarm_adapter import AlarmAdapter
from .audio import AudioEngine
from .audio_adapter import AudioAdapter
from .athan_scheduler import AthanScheduler
from .recovery import RecoveryManager, hash_code as recovery_hash_code
from .registry import RegistryEngine, RegistryError
from .registry_api import async_execute_registry_scene
from .energy import EnergyEngine
from .energy_adapter import EnergyAdapter
from .energy_runtime import (
    EnergyAutomationManager,
    EnergyController,
    EnergyFlags,
)
from .storage import ConfigError, HubStorage, JsonConfigStore, StorageError
from .tank import TankEngine
from .tls import CasaSmartTlsServer, IdentityError, ensure_tls_material
from .tunnel import (
    TUNNEL_URL_CONFIG_KEY,
    domain_to_tunnel_url,
    normalize_cloudflare_domain,
    normalize_tunnel_url,
)
from .tunnel_control import CloudflaredController, TunnelControlError
from .user_settings import UserSettingsEngine

_LOGGER = logging.getLogger(__name__)



_NOTIFY_TUNNEL_UNAVAILABLE = f"{DOMAIN}_tunnel_control_unavailable"
_NOTIFY_TUNNEL_AUTO_DISABLED = f"{DOMAIN}_tunnel_auto_disabled"
_NOTIFY_TUNNEL_ERROR = f"{DOMAIN}_tunnel_control_error"
_NOTIFY_TUNNEL_EDGE_DOWN = f"{DOMAIN}_tunnel_edge_down"
_NOTIFY_RELAY_ACTIVATION = f"{DOMAIN}_relay_activation"
_NOTIFY_RELAY_CONFIGURATION = f"{DOMAIN}_relay_configuration"







PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BUTTON,
    Platform.SENSOR,
]

type CasaSmartConfigEntry = ConfigEntry[CasaSmartRuntimeData]


@dataclass
class CasaSmartRuntimeData:
    """CasaSmart runtime component."""

    storage: HubStorage
    hub_config: JsonConfigStore
    auth: AuthEngine
    pairing: PairingManager
    recovery: RecoveryManager
    registry: RegistryEngine
    tanks: TankEngine
    user_settings: UserSettingsEngine

    push: PushTokenStore

    alarm: AlarmEngine


    audio: AudioEngine

    energy: EnergyEngine
    energy_flags: EnergyFlags


    alarm_adapter: AlarmAdapter | None = None



    audio_adapter: AudioAdapter | None = None

    energy_adapter: EnergyAdapter | None = None
    energy_controller: EnergyController | None = None


    athan_scheduler: AthanScheduler | None = None


    push_dispatcher: PushDispatcher | None = None


    relay_registrar: RelayRegistrar | None = None


    relay_config_applied: RelayConfigSnapshot | None = None


    tank_push_monitor: TankPushMonitor | None = None

    tls: CasaSmartTlsServer | None = None


    mdns: MdnsAdvertiser | None = None


    tunnel_control: CloudflaredController | None = None



    tunnel_options_applied: dict[str, Any] | None = None


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
    PushTokenStore,
    AlarmEngine,
    AudioEngine,
    EnergyEngine,
    EnergyFlags,
    str | None,
    str | None,
]:
    """CasaSmart runtime component."""
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = HubStorage(
        db_path=data_dir / DB_FILENAME,
        backup_dir=data_dir / BACKUP_DIR_NAME,
    )
    storage.open()
    hub_config = JsonConfigStore(data_dir / HUB_CONFIG_FILENAME)
    auth = AuthEngine(storage.table("auth_devices"), hub_config)
    auth.warm_up()
    pairing = PairingManager(storage.table("pairing_codes"), auth.has_admin)
    recovery = RecoveryManager(storage.table("recovery_codes"), auth.has_admin)






    bootstrap_hash = hub_config.get(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
    if bootstrap_hash:
        pairing.install_bootstrap_hash(bootstrap_hash)
        bootstrap_code = None
    else:
        bootstrap_code = pairing.ensure_bootstrap_code()
        if bootstrap_code is not None:
            hub_config.set(
                BOOTSTRAP_CODE_HASH_CONFIG_KEY, pairing_hash_code(bootstrap_code)
            )
    recovery_hash = hub_config.get(RECOVERY_CODE_HASH_CONFIG_KEY)
    if recovery_hash:
        recovery.install_recovery_hash(recovery_hash)
        recovery_code = None
    else:



        recovery_code = recovery.mint_permanent()
        hub_config.set(
            RECOVERY_CODE_HASH_CONFIG_KEY, recovery_hash_code(recovery_code)
        )




    if not hub_config.get(PROVISION_SECRET_CONFIG_KEY):
        hub_config.set(PROVISION_SECRET_CONFIG_KEY, secrets.token_urlsafe(24))
    registry = RegistryEngine(
        storage.table("registry_floors"),
        storage.table("registry_rooms"),
        storage.table("registry_devices"),
        storage.table("registry_scenes"),
        storage.table("registry_favorites"),
        storage.table("registry_user_devices"),
    )
    registry.warm_up()
    tanks = TankEngine(
        storage.table("tank_devices"),
        storage.tank_readings(),
    )
    user_settings = UserSettingsEngine(storage.table("user_settings"))
    push = PushTokenStore(storage.table("push_tokens"))



    alarm = AlarmEngine(
        storage.table("alarm_state"),
        storage.table("alarm_zones"),
        storage.table("alarm_history"),
        storage.table("alarm_settings"),
    )
    alarm.warm_up()



    audio = AudioEngine(
        storage.table("audio_config"),
        storage.table("audio_speakers"),
    )
    audio.warm_up()
    energy = EnergyEngine(
        storage.table("energy_configs"),
        storage.table("energy_state"),
        storage.energy_events(),
    )
    energy.warm_up()
    energy_flags = EnergyFlags(storage.table("energy_flags"))
    return (
        storage,
        hub_config,
        auth,
        pairing,
        recovery,
        registry,
        tanks,
        user_settings,
        push,
        alarm,
        audio,
        energy,
        energy_flags,
        bootstrap_code,
        recovery_code,
    )


async def async_migrate_entry(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> bool:
    """CasaSmart runtime component."""
    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate CasaSmart config entry version %s to %s",
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False
    if entry.version == CONFIG_ENTRY_VERSION:
        return True

    data_dir = Path(hass.config.path(DATA_DIR_NAME))
    try:
        hub_config = await hass.async_add_executor_job(
            JsonConfigStore, data_dir / HUB_CONFIG_FILENAME
        )
    except ConfigError:
        _LOGGER.error("Cannot read CasaSmart hub config during relay migration")
        return False

    legacy_value = hub_config.get(PUSH_RELAY_URL_CONFIG_KEY)
    migration = migrate_relay_options(entry.options, legacy_value)
    hass.config_entries.async_update_entry(
        entry,
        options=migration.options,
        version=CONFIG_ENTRY_VERSION,
    )

    if migration.legacy_present:
        try:
            await hass.async_add_executor_job(
                hub_config.delete, PUSH_RELAY_URL_CONFIG_KEY
            )
        except ConfigError:


            _LOGGER.warning(
                "CasaSmart relay option migrated but the legacy config key "
                "could not be removed"
            )

    if migration.base_url is None:
        persistent_notification.async_create(
            hass,
            "No valid production HTTPS push relay origin was stored. Push "
            "delivery remains disabled until Settings → Devices & services → "
            "CasaSmart Hub → Configure receives an explicit relay origin and "
            "a fresh Hub activation code.",
            title="CasaSmart Hub — relay configuration required",
            notification_id=_NOTIFY_RELAY_CONFIGURATION,
        )
    _LOGGER.info(
        "CasaSmart config entry migrated to version %s", CONFIG_ENTRY_VERSION
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> bool:
    """CasaSmart runtime component."""
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
            push,
            alarm,
            audio,
            energy,
            energy_flags,
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
        push=push,
        alarm=alarm,
        audio=audio,
        energy=energy,
        energy_flags=energy_flags,
        relay_config_applied=relay_config_snapshot(entry.options, entry.data),
    )





    async def _async_close_storage_on_stop(_event: Event) -> None:
        await hass.async_add_executor_job(storage.close)
        _LOGGER.info("CasaSmart Hub storage checkpointed and closed on HA stop")

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            _async_close_storage_on_stop,
        )
    )




    await _async_sync_tunnel_url(hass, entry)

    await _async_import_registry(hass, hub_config, registry)








    await _async_setup_dev_enroll(hass, entry, data_dir)

    if bootstrap_code is not None:


        persistent_notification.async_create(
            hass,
            f"Initial admin pairing code: **{bootstrap_code}**\n\n"
            "Use it in the CasaSmart app (on this network) to claim the "
            "hub. It stays valid until an admin is paired.",
            title="CasaSmart Hub — pairing code",
            notification_id=f"{DOMAIN}_bootstrap_pairing",
        )

    if recovery_code is not None:


        notify_recovery_code(hass, recovery_code)

    _async_register_services(hass)


    integration = await async_get_integration(hass, DOMAIN)
    hub_version = str(integration.version) if integration.version else "0.0.0"
    async_register_views(hass, hub_version=hub_version)

    await _async_start_tls(hass, entry, data_dir, hub_version)
    await _async_start_mdns(hass, entry)
    await _async_start_push(hass, entry, data_dir)



    alarm_adapter = AlarmAdapter(hass, alarm)
    alarm_adapter.async_start()
    entry.runtime_data.alarm_adapter = alarm_adapter



    energy_adapter = EnergyAdapter(
        hass,
        energy,
        registry,
        change_callback=lambda: hass.bus.async_fire(EVENT_ENERGY_CHANGED),
    )
    energy_automations = EnergyAutomationManager(hass, energy, energy_flags)
    energy_controller = EnergyController(
        hass, energy, energy_adapter, energy_automations
    )
    entry.runtime_data.energy_adapter = energy_adapter
    entry.runtime_data.energy_controller = energy_controller
    await energy_controller.async_start()




    audio_adapter = AudioAdapter(hass, audio)
    await audio_adapter.async_start()
    entry.runtime_data.audio_adapter = audio_adapter




    athan_scheduler = AthanScheduler(hass, audio, audio_adapter)
    await athan_scheduler.async_start()
    entry.runtime_data.athan_scheduler = athan_scheduler



    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)





    entry.runtime_data.tunnel_control = CloudflaredController(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_create_background_task(
        hass,
        _async_reconcile_tunnel(hass, entry),
        name="casasmart-tunnel-reconcile",
    )




    async def _async_run_tunnel_watchdog(_now) -> None:
        await _async_tunnel_watchdog(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_run_tunnel_watchdog,
            timedelta(minutes=TUNNEL_WATCHDOG_INTERVAL_MINUTES),
        )
    )

    _LOGGER.info("CasaSmart Hub storage ready at %s", data_dir)
    return True


async def _async_import_registry(
    hass: HomeAssistant,
    hub_config: JsonConfigStore,
    registry: RegistryEngine,
) -> None:
    """CasaSmart runtime component."""
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
    except Exception:


        _LOGGER.exception("Registry seed failed — will retry on next start")
        return
    _LOGGER.info(
        "Registry seeded from HA: %d floors, %d rooms, %d assignments",
        counts["floors"],
        counts["rooms"],
        counts["assignments"],
    )










_DEV_ENROLL_ENV = "CASASMART_DEV_ENROLL"


def _dev_enroll_enabled() -> bool:
    """CasaSmart runtime component."""
    return os.environ.get(_DEV_ENROLL_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _async_setup_dev_enroll(
    hass: HomeAssistant, entry: CasaSmartConfigEntry, data_dir: Path
) -> None:
    """CasaSmart runtime component."""
    if not _dev_enroll_enabled():
        return

    auth = entry.runtime_data.auth

    async def _provision() -> None:
        await hass.async_add_executor_job(ensure_dev_devices, data_dir, auth)

    await _provision()

    @callback
    def _on_auth_changed(_event) -> None:
        entry.async_create_task(hass, _provision())

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_AUTH_CHANGED, _on_auth_changed)
    )


async def _async_start_tls(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    data_dir: Path,
    hub_version: str,
) -> None:
    """CasaSmart runtime component."""
    runtime_data = entry.runtime_data
    try:
        material = await hass.async_add_executor_job(ensure_tls_material, data_dir)
    except IdentityError as err:


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
    """CasaSmart runtime component."""
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


async def _async_start_push(
    hass: HomeAssistant, entry: CasaSmartConfigEntry, data_dir: Path
) -> None:
    """CasaSmart runtime component."""
    runtime_data = entry.runtime_data
    if runtime_data.tls is None:
        _LOGGER.info("Push dispatcher skipped — TLS identity unavailable")
        return

    relay_config = relay_config_snapshot(entry.options, entry.data)
    runtime_data.relay_config_applied = relay_config
    if relay_config.base_url is None:
        persistent_notification.async_create(
            hass,
            "Push delivery is disabled because no production HTTPS relay "
            "origin is configured. Open Settings → Devices & services → "
            "CasaSmart Hub → Configure and submit an explicit relay origin "
            "with a fresh Hub activation code.",
            title="CasaSmart Hub — relay configuration required",
            notification_id=_NOTIFY_RELAY_CONFIGURATION,
        )
        return

    try:
        signer = await hass.async_add_executor_job(
            ensure_push_identity, data_dir, runtime_data.hub_config
        )
    except PushIdentityError:
        _LOGGER.exception("Push dispatcher skipped — push-identity key unusable")
        return

    endpoints = relay_endpoints(relay_config.base_url)
    activation_code_raw = entry.data.get(CONF_RELAY_ACTIVATION_CODE)
    activation_code = (
        activation_code_raw.strip()
        if isinstance(activation_code_raw, str)
        and is_activation_code_format(activation_code_raw.strip())
        else None
    )

    dispatcher = PushDispatcher(
        hass,
        push_store=runtime_data.push,
        signer=signer,
        hub_id=runtime_data.tls.material.identity_fingerprint,
        relay_url=endpoints.push_url,
        session=async_get_clientsession(hass),
    )
    dispatcher.async_start()
    runtime_data.push_dispatcher = dispatcher

    async def _async_registration_ready() -> None:
        """CasaSmart runtime component."""
        if (
            CONF_RELAY_ACTIVATION_CODE in entry.data
            or CONF_RELAY_ACTIVATION_REQUEST_ID in entry.data
        ):
            new_data = without_relay_activation(entry.data)
            hass.config_entries.async_update_entry(entry, data=new_data)
        persistent_notification.async_dismiss(hass, _NOTIFY_RELAY_ACTIVATION)
        persistent_notification.async_dismiss(hass, _NOTIFY_RELAY_CONFIGURATION)

    async def _async_registration_failed(reason: str) -> None:
        """CasaSmart runtime component."""
        persistent_notification.async_create(
            hass,
            "Automatic push-relay enrollment stopped safely: "
            f"**{reason}**. Generate a fresh Hub activation code, then open "
            "Settings → Devices & services → CasaSmart Hub → Configure and "
            "submit it against the displayed relay server. Local CasaSmart "
            "operation is unaffected.",
            title="CasaSmart Hub activation required",
            notification_id=_NOTIFY_RELAY_ACTIVATION,
        )




    registrar = RelayRegistrar(
        session=async_get_clientsession(hass),
        registration_url=endpoints.registration_url,
        hub_id=runtime_data.tls.material.identity_fingerprint,
        identity_signer=runtime_data.tls.material.identity_signer,
        push_signer=signer,
        activation_code=activation_code,
        on_success=_async_registration_ready,
        on_permanent_failure=_async_registration_failed,
    )
    registrar.start(hass, entry)
    runtime_data.relay_registrar = registrar




    tank_monitor = TankPushMonitor(
        hass, tanks=runtime_data.tanks, notifier=dispatcher
    )
    tank_monitor.async_start()
    runtime_data.tank_push_monitor = tank_monitor


def _tunnel_options_snapshot(entry: CasaSmartConfigEntry) -> dict[str, Any]:
    """CasaSmart runtime component."""
    return {
        CONF_CLOUDFLARE_DOMAIN: entry.options.get(CONF_CLOUDFLARE_DOMAIN),
        CONF_TUNNEL_ENABLED: entry.options.get(CONF_TUNNEL_ENABLED),
    }


async def _async_sync_tunnel_url(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """CasaSmart runtime component."""
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    runtime_data = entry.runtime_data
    if domain:
        url = domain_to_tunnel_url(domain)
        if url is None:

            _LOGGER.warning(
                "Configured Cloudflare domain %r is unusable — not advertising it",
                domain,
            )
        elif runtime_data.hub_config.get(TUNNEL_URL_CONFIG_KEY) != url:


            await hass.async_add_executor_job(
                runtime_data.hub_config.set, TUNNEL_URL_CONFIG_KEY, url
            )
            _LOGGER.info(
                "Advertised tunnel URL derived from Cloudflare domain: %s", url
            )
    runtime_data.tunnel_options_applied = _tunnel_options_snapshot(entry)


async def _async_options_updated(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """CasaSmart runtime component."""
    runtime_data = entry.runtime_data
    applied_relay = runtime_data.relay_config_applied
    configured_relay = normalize_relay_base_url(
        entry.options.get(CONF_PUSH_RELAY_URL)
    )
    activation_raw = entry.data.get(CONF_RELAY_ACTIVATION_CODE)
    activation_code = (
        activation_raw.strip() if isinstance(activation_raw, str) else ""
    )
    activation_present = bool(activation_code)
    activation_valid = is_activation_code_format(activation_code)

    if applied_relay is not None and (
        configured_relay is None or (activation_present and not activation_valid)
    ):
        new_options = dict(entry.options)
        if applied_relay.base_url is None:
            new_options.pop(CONF_PUSH_RELAY_URL, None)
        else:
            new_options[CONF_PUSH_RELAY_URL] = applied_relay.base_url
        new_data = (
            without_relay_activation(entry.data)
            if activation_present and not activation_valid
            else dict(entry.data)
        )
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
        )
        persistent_notification.async_create(
            hass,
            "The push relay update was not applied because its server or Hub "
            "activation code was invalid. Open Settings → Devices & services → "
            "CasaSmart Hub → Configure and try again. The previous relay "
            "remains selected.",
            title="CasaSmart Hub — invalid relay setting",
            notification_id=_NOTIFY_RELAY_CONFIGURATION,
        )
        _LOGGER.warning("CasaSmart rejected an invalid relay configuration update")
        return

    requested_relay = relay_config_snapshot(entry.options, entry.data)
    if applied_relay is not None and relay_reload_required(
        applied_relay, requested_relay
    ):
        relay_changed = requested_relay.base_url != applied_relay.base_url

        if not activation_valid:


            new_options = dict(entry.options)
            if relay_changed:
                if applied_relay.base_url is None:
                    new_options.pop(CONF_PUSH_RELAY_URL, None)
                else:
                    new_options[CONF_PUSH_RELAY_URL] = applied_relay.base_url
            new_data = without_relay_activation(entry.data)
            hass.config_entries.async_update_entry(
                entry,
                data=new_data,
                options=new_options,
            )
            persistent_notification.async_create(
                hass,
                "The push relay change was not applied. Changing servers or "
                "re-registering requires a complete fresh Hub activation code. "
                "Open Settings → Devices & services → CasaSmart Hub → Configure "
                "and try again. The previous relay remains selected.",
                title="CasaSmart Hub — relay change rejected",
                notification_id=_NOTIFY_RELAY_CONFIGURATION,
            )
            _LOGGER.warning(
                "CasaSmart relay update rejected because no valid activation "
                "credential was present"
            )
            return



        reloaded = await async_reload_relay_runtime(hass, entry)
        if not reloaded:
            _LOGGER.error(
                "CasaSmart could not reload after a relay configuration update"
            )
        if not reloaded:
            persistent_notification.async_create(
                hass,
                "The new push relay setting was saved, but CasaSmart could not "
                "reload it. Push delivery is paused so the old server cannot be "
                "used. Reload the CasaSmart Hub integration, then open Configure "
                "again if registration still needs recovery.",
                title="CasaSmart Hub — relay reload required",
                notification_id=_NOTIFY_RELAY_CONFIGURATION,
            )
        else:
            persistent_notification.async_dismiss(
                hass, _NOTIFY_RELAY_CONFIGURATION
            )
        return

    previous = runtime_data.tunnel_options_applied or {}
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    previous_domain = previous.get(CONF_CLOUDFLARE_DOMAIN)

    if domain:
        await _async_sync_tunnel_url(hass, entry)
    else:
        if previous_domain:



            derived = domain_to_tunnel_url(previous_domain)
            hub_config = runtime_data.hub_config
            if (
                derived is not None
                and hub_config.get(TUNNEL_URL_CONFIG_KEY) == derived
            ):
                await hass.async_add_executor_job(
                    hub_config.delete, TUNNEL_URL_CONFIG_KEY
                )
                _LOGGER.info(
                    "Cloudflare domain cleared — no longer advertising %s",
                    derived,
                )
        runtime_data.tunnel_options_applied = _tunnel_options_snapshot(entry)

    entry.async_create_background_task(
        hass,
        _async_reconcile_tunnel(hass, entry),
        name="casasmart-tunnel-reconcile",
    )


async def _async_reconcile_tunnel(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """CasaSmart runtime component."""
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    if not domain:


        return
    desired_on = bool(entry.options.get(CONF_TUNNEL_ENABLED, False))

    controller = entry.runtime_data.tunnel_control
    if controller is None:
        return

    if not controller.available():


        _LOGGER.info(
            "Cloudflare domain configured but tunnel control is unavailable "
            "(no add-on Supervisor on this install) — manage cloudflared "
            "manually; the options toggle has no effect here"
        )
        persistent_notification.async_create(
            hass,
            "A Cloudflare tunnel domain is configured, but this Home "
            "Assistant install has no add-on Supervisor, so the CasaSmart "
            "hub cannot start/stop cloudflared for you. Manage the tunnel "
            "where it runs; the domain keeps being advertised to phones.",
            title="CasaSmart — tunnel control unavailable",
            notification_id=_NOTIFY_TUNNEL_UNAVAILABLE,
        )
        return

    try:
        slug = await controller.async_discover()
        if slug is None:
            _LOGGER.warning(
                "Cloudflare domain configured but no cloudflared add-on is "
                "installed — desired tunnel state (%s) saved; it will be "
                "applied once the add-on is installed",
                "enabled" if desired_on else "disabled",
            )
            persistent_notification.async_create(
                hass,
                "A Cloudflare tunnel domain is configured, but no cloudflared "
                "add-on is installed. Install the Cloudflare Tunnel add-on and "
                "the CasaSmart hub will manage it automatically.",
                title="CasaSmart — cloudflared add-on not found",
                notification_id=_NOTIFY_TUNNEL_UNAVAILABLE,
            )
            return

        state = await controller.async_state(slug)
        if desired_on:
            if not state.running or state.boot != "auto":
                await controller.async_enable(slug, running=state.running)
        else:
            if state.running or state.boot != "manual":
                await controller.async_disable(slug, running=state.running)
            if state.running:

                persistent_notification.async_create(
                    hass,
                    f"The Cloudflare tunnel add-on ({slug}) was stopped and "
                    "set to manual start. Device pairing must happen over the "
                    "LAN only — an active tunnel can route even local phones "
                    "through Cloudflare, where the hub's LAN-only gate blocks "
                    "them. Re-enable the tunnel from the CasaSmart "
                    "integration options (gear icon) once pairing is done.",
                    title="CasaSmart — Cloudflare tunnel disabled",
                    notification_id=_NOTIFY_TUNNEL_AUTO_DISABLED,
                )
    except TunnelControlError as err:
        _LOGGER.warning("Cloudflare tunnel reconcile failed: %s", err)
        persistent_notification.async_create(
            hass,
            f"Could not reconcile the Cloudflare tunnel add-on: {err}\n\n"
            "The hub keeps running and the tunnel was left as-is. Check the "
            "Supervisor, then save the CasaSmart integration options again "
            "to retry.",
            title="CasaSmart — tunnel control error",
            notification_id=_NOTIFY_TUNNEL_ERROR,
        )
        return

    persistent_notification.async_dismiss(hass, _NOTIFY_TUNNEL_ERROR)


async def _async_tunnel_watchdog(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """CasaSmart runtime component."""
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    if not domain or not bool(entry.options.get(CONF_TUNNEL_ENABLED, False)):
        return
    controller = entry.runtime_data.tunnel_control
    if controller is None or not controller.available():
        return
    tunnel_url = entry.runtime_data.hub_config.get(TUNNEL_URL_CONFIG_KEY)
    if not isinstance(tunnel_url, str) or not tunnel_url:
        return

    try:
        slug = await controller.async_discover()
        if slug is None:
            return
        state = await controller.async_state(slug)
        if not state.running:


            return
        result = await controller.async_watchdog_check(
            slug, tunnel_url, time.monotonic()
        )
    except TunnelControlError as err:
        _LOGGER.warning("Cloudflare tunnel watchdog failed: %s", err)
        return

    if result == "restart":
        _LOGGER.warning(
            "cloudflared %s was running but its Cloudflare edge connection was "
            "down — restarted it to restore remote access",
            slug,
        )
        persistent_notification.async_create(
            hass,
            f"The Cloudflare tunnel add-on ({slug}) was running but had lost "
            "its connection to Cloudflare's edge, so remote access was down. "
            "The hub restarted it automatically to reconnect. If this repeats, "
            "check the add-on logs and your Cloudflare tunnel credentials.",
            title="CasaSmart — tunnel auto-recovered",
            notification_id=_NOTIFY_TUNNEL_EDGE_DOWN,
        )
    elif result == "up":

        persistent_notification.async_dismiss(hass, _NOTIFY_TUNNEL_EDGE_DOWN)


def _async_register_services(hass: HomeAssistant) -> None:
    """CasaSmart runtime component."""
    if all(
        hass.services.has_service(DOMAIN, service)
        for service in ("factory_reset", "set_tunnel_url", "activate_scene")
    ):
        return

    async def _handle_activate_scene(call) -> None:
        """CasaSmart runtime component."""
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("CasaSmart hub is not loaded")
        scene_id = call.data.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id:
            raise HomeAssistantError("scene_id is required")
        registry = entries[0].runtime_data.registry
        try:
            scene = await hass.async_add_executor_job(registry.get_scene, scene_id)
        except RegistryError as err:
            raise HomeAssistantError(str(err)) from err
        runtime_data = entries[0].runtime_data
        energy = getattr(runtime_data, "energy", None)
        if (
            energy is not None
            and energy.active_level is not None
            and not scene.get("works_during_energy_saving", False)
        ):
            raise HomeAssistantError(
                "Scene is disabled while Energy Saving is active"
            )
        result = await async_execute_registry_scene(hass, scene)
        if not result["ok"]:
            failed = [
                item["entity_id"]
                for item in result["results"]
                if not item["ok"]
            ]
            raise HomeAssistantError(
                f"Scene {scene_id} failed for: {', '.join(failed)}"
            )

    async def _handle_set_tunnel_url(call) -> None:
        """CasaSmart runtime component."""
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("CasaSmart hub is not loaded")
        entry = entries[0]
        runtime_data: CasaSmartRuntimeData = entry.runtime_data

        url = normalize_tunnel_url(call.data.get("url"))
        if url is None:
            raise HomeAssistantError(
                "Invalid tunnel URL — must be a plain https origin "
                "(no userinfo/query/fragment)"
            )

        await hass.async_add_executor_job(
            runtime_data.hub_config.set, TUNNEL_URL_CONFIG_KEY, url
        )
        _LOGGER.info(
            "CasaSmart tunnel URL set to %s — advertised on the next handshake",
            url,
        )




        runtime_data.tunnel_options_applied = _tunnel_options_snapshot(entry)

        domain = normalize_cloudflare_domain(url)
        if domain is not None and entry.options.get(CONF_CLOUDFLARE_DOMAIN) != domain:
            new_options = dict(entry.options)
            new_options[CONF_CLOUDFLARE_DOMAIN] = domain
            new_options.setdefault(CONF_TUNNEL_ENABLED, True)

            hass.config_entries.async_update_entry(entry, options=new_options)
        elif domain is None:


            _LOGGER.debug(
                "Tunnel URL %s is not a bare origin — not mirrored to options",
                url,
            )

    async def _handle_factory_reset(call) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("CasaSmart hub is not loaded")
        runtime_data: CasaSmartRuntimeData = entries[0].runtime_data




        if runtime_data.energy_controller is not None:
            await runtime_data.energy_controller.async_deactivate(
                actor="factory_reset"
            )
        pending_automations = await hass.async_add_executor_job(
            runtime_data.energy_flags.disabled_automations
        )
        if pending_automations:
            raise HomeAssistantError(
                "Factory reset paused because Energy Saving could not restore: "
                + ", ".join(pending_automations)
            )

        def _wipe() -> None:
            runtime_data.storage.table("auth_devices").clear()
            runtime_data.storage.table("pairing_codes").clear()
            runtime_data.storage.table("recovery_codes").clear()



            runtime_data.storage.table("registry_favorites").clear()





            runtime_data.storage.table("registry_scenes").clear()



            runtime_data.storage.table("user_settings").clear()

            runtime_data.storage.table("push_tokens").clear()



            runtime_data.storage.table("alarm_history").clear()
            runtime_data.storage.table("alarm_state").clear()




            runtime_data.storage.table("audio_config").clear()
            runtime_data.storage.table("audio_speakers").clear()



            runtime_data.storage.table("energy_configs").clear()
            runtime_data.storage.table("energy_state").clear()
            runtime_data.storage.table("energy_flags").clear()
            runtime_data.storage.energy_events().clear()








            runtime_data.storage.table("registry_floors").clear()
            runtime_data.storage.table("registry_rooms").clear()
            runtime_data.storage.table("registry_devices").clear()
            runtime_data.storage.table("registry_user_devices").clear()
            runtime_data.hub_config.delete("registry_imported")




            runtime_data.hub_config.delete(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
            runtime_data.hub_config.delete(RECOVERY_CODE_HASH_CONFIG_KEY)

        await hass.async_add_executor_job(_wipe)
        _LOGGER.warning(
            "CasaSmart factory reset (full blank): wiped devices, pairing, "
            "recovery, favorites, scenes, settings, push, alarm log/state, "
            "audio config + speakers, Energy Saving data, and the registry "
            "org layer (floors/rooms/"
            "assignments/grouping) — re-seeding from HA on reload; printed "
            "codes rotated"
        )


        await hass.config_entries.async_reload(entries[0].entry_id)

    if not hass.services.has_service(DOMAIN, "factory_reset"):
        hass.services.async_register(DOMAIN, "factory_reset", _handle_factory_reset)
    if not hass.services.has_service(DOMAIN, "set_tunnel_url"):
        hass.services.async_register(DOMAIN, "set_tunnel_url", _handle_set_tunnel_url)
    if not hass.services.has_service(DOMAIN, "activate_scene"):
        hass.services.async_register(DOMAIN, "activate_scene", _handle_activate_scene)


async def async_unload_entry(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> bool:
    """CasaSmart runtime component."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if entry.runtime_data.energy_controller is not None:
        entry.runtime_data.energy_controller.async_stop()
    if entry.runtime_data.alarm_adapter is not None:
        entry.runtime_data.alarm_adapter.async_stop()
    if entry.runtime_data.tank_push_monitor is not None:
        entry.runtime_data.tank_push_monitor.async_stop()
    if entry.runtime_data.relay_registrar is not None:
        entry.runtime_data.relay_registrar.stop()
    if entry.runtime_data.push_dispatcher is not None:
        entry.runtime_data.push_dispatcher.async_stop()
    if entry.runtime_data.athan_scheduler is not None:
        await entry.runtime_data.athan_scheduler.async_stop()
    if entry.runtime_data.audio_adapter is not None:
        await entry.runtime_data.audio_adapter.async_stop()
    if entry.runtime_data.mdns is not None:
        await entry.runtime_data.mdns.async_stop()
    if entry.runtime_data.tls is not None:
        await entry.runtime_data.tls.async_stop()
    await hass.async_add_executor_job(entry.runtime_data.storage.close)
    _LOGGER.info("CasaSmart Hub storage closed")
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """CasaSmart runtime component."""
    if not entry.options.get(CONF_CLOUDFLARE_DOMAIN):
        return
    controller = CloudflaredController(hass)
    if not controller.available():
        return
    try:
        slug = await controller.async_discover()
        if slug is not None:
            await controller.async_restore_boot_auto(slug)
            _LOGGER.info(
                "CasaSmart removed — cloudflared add-on %s restored to "
                "boot=auto",
                slug,
            )
    except TunnelControlError as err:
        _LOGGER.warning(
            "Could not restore cloudflared boot mode on removal: %s", err
        )
