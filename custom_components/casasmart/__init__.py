"""CasaSmart Hub integration: config-entry setup, teardown and services.

Setup opens the hub's storage and engines, makes sure the owner's pairing and
recovery codes exist, registers the REST/WebSocket views and the
casasmart.* services, and starts the runtimes: the hub's own TLS listener,
mDNS discovery, the push relay leg, the Home Assistant adapters (alarm, energy,
audio, athan) and the Cloudflare tunnel reconciler. Blocking storage work runs
in the executor.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.loader import async_get_integration

from .alarm import AlarmEngine
from .alarm_adapter import AlarmAdapter
from .api import async_register_views, build_views
from .athan_scheduler import AthanScheduler
from .audio import AudioEngine
from .audio_adapter import AudioAdapter
from .auth_api import notify_recovery_code
from .auth_engine import AuthEngine
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
    FACTORY_RESET_TABLES,
    HUB_CONFIG_FILENAME,
    HUB_NAME_CONFIG_KEY,
    KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY,
    MDNS_REFRESH_INTERVAL_MINUTES,
    PROVISION_SECRET_CONFIG_KEY,
    PUSH_RELAY_URL_CONFIG_KEY,
    RECOVERY_CODE_HASH_CONFIG_KEY,
    TLS_CERT_CHECK_INTERVAL_HOURS,
    TLS_PORT_DEFAULT,
    TUNNEL_WATCHDOG_INTERVAL_MINUTES,
)
from .dev_enroll import ensure_dev_devices
from .discovery import MdnsAdvertiser
from .energy import EnergyEngine
from .energy_adapter import EnergyAdapter
from .energy_runtime import (
    EnergyAutomationManager,
    EnergyController,
    EnergyFlags,
)
from .entity_bridge import is_exposed
from .hq_notifications import (
    HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY,
    HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY,
    HQ_SENDER_NAME_MAX_LENGTH,
    HqNotificationError,
    normalize_public_key,
    normalize_sender_name,
)
from .lan_ingress import (
    LAN_RELAY_INGRESS_CONFIG_KEY,
    is_recognized_lan_relay_ingress,
    needs_relay_ingress_hint,
    resolve_lan_relay_ingress,
)
from .now_data import NowDataEngine
from .pairing import PairingManager
from .pairing import hash_code as pairing_hash_code
from .push import PushTokenStore
from .push_crypto import PushIdentityError, ensure_push_identity
from .push_dispatcher import PushDispatcher, TankPushMonitor
from .recovery import RecoveryManager
from .recovery import hash_code as recovery_hash_code
from .registry import RegistryEngine, RegistryError
from .registry_api import async_execute_registry_scene
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
from .runtime_lookup import loaded_entry
from .storage import ConfigError, HubStorage, JsonConfigStore, StorageError
from .suggestion_runtime import SuggestionRuntime
from .suggestion_store import SuggestionStore
from .tank import TankEngine
from .tls import CasaSmartTlsServer, IdentityError, ensure_tls_material
from .tunnel import (
    TUNNEL_URL_CONFIG_KEY,
    domain_to_tunnel_url,
    normalize_cloudflare_domain,
    normalize_tunnel_url,
)
from .tunnel_control import CloudflaredController, TunnelControlError
from .update_install import async_clear_legacy_update_dirs
from .user_settings import UserSettingsEngine
from .ws import async_close_connections

_LOGGER = logging.getLogger(__name__)

# Persistent-notification ids, so a later run can replace or dismiss each one.
_NOTIFY_TUNNEL_UNAVAILABLE = f"{DOMAIN}_tunnel_control_unavailable"
_NOTIFY_TUNNEL_AUTO_DISABLED = f"{DOMAIN}_tunnel_auto_disabled"
_NOTIFY_TUNNEL_ERROR = f"{DOMAIN}_tunnel_control_error"
_NOTIFY_TUNNEL_EDGE_DOWN = f"{DOMAIN}_tunnel_edge_down"
_NOTIFY_RELAY_ACTIVATION = f"{DOMAIN}_relay_activation"
_NOTIFY_RELAY_CONFIGURATION = f"{DOMAIN}_relay_configuration"

# Developer seam (dev_enroll.py): off unless this environment variable is set.
_DEV_ENROLL_ENV = "CASASMART_DEV_ENROLL"

# A hub_config key retired in 2.3.0; setup warns while it is still present.
_RETIRED_EXTRA_LAN_CIDRS_KEY = "pairing_extra_lan_cidrs"


PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BUTTON,
    Platform.SENSOR,
]

type CasaSmartConfigEntry = ConfigEntry[CasaSmartRuntimeData]


@dataclass
class CasaSmartRuntimeData:
    """Everything one loaded config entry owns, kept on entry.runtime_data.

    The required fields are the storage-backed engines from _open_storage.
    The optional ones are runtimes started later in setup; each stays None
    while its feature is off or failed to start, and teardown skips it.
    """

    storage: HubStorage
    hub_config: JsonConfigStore
    auth: AuthEngine
    pairing: PairingManager
    recovery: RecoveryManager
    registry: RegistryEngine
    tanks: TankEngine
    user_settings: UserSettingsEngine
    now_data: NowDataEngine

    push: PushTokenStore

    alarm: AlarmEngine

    audio: AudioEngine

    energy: EnergyEngine
    energy_flags: EnergyFlags

    alarm_adapter: AlarmAdapter | None = None
    suggestions: SuggestionRuntime | None = None

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
) -> tuple[CasaSmartRuntimeData, str | None, str | None]:
    """Open the database and hub config and build every storage-backed engine.

    Blocking: run it in the executor. Also reinstalls the owner's pairing and
    recovery codes from their hashes in hub_config, minting a code only when
    its hash is missing (first start, or after a factory reset). Returns the
    runtime data and those new plaintext pairing and recovery codes, or None;
    this is the only time they exist in the clear. If anything after opening
    the database fails, it is closed again before the error propagates.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = HubStorage(
        db_path=data_dir / DB_FILENAME,
        backup_dir=data_dir / BACKUP_DIR_NAME,
    )
    storage.open()
    try:
        return _build_runtime_data(storage, data_dir)
    except BaseException:
        storage.close()
        raise


def _build_runtime_data(
    storage: HubStorage, data_dir: Path
) -> tuple[CasaSmartRuntimeData, str | None, str | None]:
    """_open_storage's work once the database is open (blocking)."""
    hub_config = JsonConfigStore(data_dir / HUB_CONFIG_FILENAME)
    auth = AuthEngine(storage.table("auth_devices"), hub_config)
    auth.warm_up()
    pairing = PairingManager(storage.table("pairing_codes"), auth.has_admin)
    recovery = RecoveryManager(
        storage.table("recovery_codes"),
        auth.has_admin,
        save_hash=lambda code_hash: hub_config.set(
            RECOVERY_CODE_HASH_CONFIG_KEY, code_hash
        ),
    )

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
        hub_config.set(RECOVERY_CODE_HASH_CONFIG_KEY, recovery_hash_code(recovery_code))

    # The key speakers present to fetch their broker settings; created once.
    if not hub_config.get(PROVISION_SECRET_CONFIG_KEY):
        hub_config.set(PROVISION_SECRET_CONFIG_KEY, secrets.token_urlsafe(24))
    registry = RegistryEngine(
        storage.table("registry_floors"),
        storage.table("registry_rooms"),
        storage.table("registry_devices"),
        storage.table("registry_scenes"),
        storage.table("registry_favorites"),
        storage.table("registry_user_devices"),
        storage.table("registry_room_tags"),
    )
    registry.warm_up()
    tanks = TankEngine(
        storage.table("tank_devices"),
        storage.tank_readings(),
    )
    user_settings = UserSettingsEngine(storage.table("user_settings"))
    now_data = NowDataEngine(
        storage.table("now_recents"),
        storage.table("now_room_policies"),
        storage.table("now_config"),
        storage.table("now_restore_sets"),
        storage.table("now_idempotency"),
    )
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
    runtime_data = CasaSmartRuntimeData(
        storage=storage,
        hub_config=hub_config,
        auth=auth,
        pairing=pairing,
        recovery=recovery,
        registry=registry,
        tanks=tanks,
        user_settings=user_settings,
        now_data=now_data,
        push=push,
        alarm=alarm,
        audio=audio,
        energy=energy,
        energy_flags=energy_flags,
    )
    return runtime_data, bootstrap_code, recovery_code


async def async_migrate_entry(hass: HomeAssistant, entry: CasaSmartConfigEntry) -> bool:
    """Upgrade an older config entry to CONFIG_ENTRY_VERSION.

    Version 3 moved the push relay URL from hub_config into the entry options.
    A stored URL that isn't a usable production HTTPS origin is not carried
    over: push stays off, and a notification asks for a relay and a fresh
    activation code.
    """
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
    _LOGGER.info("CasaSmart config entry migrated to version %s", CONFIG_ENTRY_VERSION)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CasaSmartConfigEntry) -> bool:
    """Set up the hub from its config entry.

    Storage and the engines come first. The TLS listener starts before mDNS
    and push, which both need its identity fingerprint. Optional parts (the
    TLS port, mDNS, push, the tunnel) log their failures and the rest of the
    hub keeps running. Storage that won't open is retried by Home Assistant;
    a corrupt identity key stops setup until a person fixes it. Any failure
    once storage is open stops what already started and closes storage.
    """
    data_dir = Path(hass.config.path(DATA_DIR_NAME))
    # Older self-updates left copies of the integration in custom_components,
    # and Home Assistant could load one of those instead of this one.
    await async_clear_legacy_update_dirs(hass)

    try:
        runtime_data, bootstrap_code, recovery_code = await hass.async_add_executor_job(
            _open_storage, data_dir
        )
    except StorageError as err:
        raise ConfigEntryNotReady(f"CasaSmart storage failed to open: {err}") from err
    runtime_data.relay_config_applied = relay_config_snapshot(entry.options, entry.data)
    entry.runtime_data = runtime_data

    try:
        await _async_start_hub(hass, entry, data_dir, bootstrap_code, recovery_code)
    except BaseException:
        # A failed setup gets no async_unload_entry, so nothing else stops
        # these, and a retry would run a second copy beside them.
        try:
            await _async_stop_hub(hass, runtime_data)
        except Exception:
            _LOGGER.exception("CasaSmart Hub did not stop cleanly after failing")
        raise
    _LOGGER.info("CasaSmart Hub storage ready at %s", data_dir)
    return True


async def _async_start_hub(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    data_dir: Path,
    bootstrap_code: str | None,
    recovery_code: str | None,
) -> None:
    """Everything setup does once storage is open.

    Each runtime goes on runtime_data before it starts, so _async_stop_hub
    can stop one that fails part-way through starting.
    """
    runtime_data = entry.runtime_data
    suggestion_store = SuggestionStore(runtime_data.storage)
    # Marks suggestion runs a restart interrupted as unknown; never reruns them.
    await hass.async_add_executor_job(suggestion_store.recover)
    entry.runtime_data.suggestions = SuggestionRuntime(
        hass, suggestion_store, runtime_data.registry, now_data=runtime_data.now_data
    )
    await entry.runtime_data.suggestions.start()
    entry.async_on_unload(entry.runtime_data.suggestions.stop)

    # Home Assistant doesn't unload config entries when it stops, so the
    # database is checkpointed and closed here instead.
    async def _async_close_storage_on_stop(_event: Event) -> None:
        entry.runtime_data.suggestions.stop()
        await hass.async_add_executor_job(runtime_data.storage.close)
        _LOGGER.info("CasaSmart Hub storage checkpointed and closed on HA stop")

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            _async_close_storage_on_stop,
        )
    )

    await _async_sync_tunnel_url(hass, entry)

    await _async_import_registry(hass, runtime_data.hub_config, runtime_data.registry)

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

    _warn_keyless_speaker_provisioning(runtime_data.hub_config, data_dir)

    _async_register_services(hass)

    integration = await async_get_integration(hass, DOMAIN)
    hub_version = str(integration.version) if integration.version else "0.0.0"
    async_register_views(hass, hub_version=hub_version)

    await _async_start_tls(hass, entry, data_dir, hub_version)
    await _async_start_mdns(hass, entry)
    await _async_start_push(hass, entry, data_dir)

    alarm_adapter = AlarmAdapter(hass, runtime_data.alarm)
    entry.runtime_data.alarm_adapter = alarm_adapter
    alarm_adapter.async_start()

    energy = runtime_data.energy
    energy_adapter = EnergyAdapter(
        hass,
        energy,
        runtime_data.registry,
        change_callback=lambda: hass.bus.async_fire(EVENT_ENERGY_CHANGED),
    )
    energy_automations = EnergyAutomationManager(
        hass, energy, runtime_data.energy_flags
    )
    energy_controller = EnergyController(
        hass, energy, energy_adapter, energy_automations
    )
    entry.runtime_data.energy_adapter = energy_adapter
    entry.runtime_data.energy_controller = energy_controller
    await energy_controller.async_start()

    audio_adapter = AudioAdapter(hass, runtime_data.audio)
    entry.runtime_data.audio_adapter = audio_adapter
    await audio_adapter.async_start()

    athan_scheduler = AthanScheduler(hass, runtime_data.audio, audio_adapter)
    entry.runtime_data.athan_scheduler = athan_scheduler
    await athan_scheduler.async_start()

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


async def _async_import_registry(
    hass: HomeAssistant,
    hub_config: JsonConfigStore,
    registry: RegistryEngine,
) -> None:
    """Seed the registry from HA's floors, areas and entity areas on first run.

    Rooms keep their HA area ids, which room-scoped tokens already use. The
    registry_imported flag makes this run once, and import_initial never
    overwrites a record, so a retry after a failed seed only fills the gaps.
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
    except Exception:
        # The flag stays unset, so the next start retries.
        _LOGGER.exception("Registry seed failed — will retry on next start")
        return
    _LOGGER.info(
        "Registry seeded from HA: %d floors, %d rooms, %d assignments",
        counts["floors"],
        counts["rooms"],
        counts["assignments"],
    )


def _dev_enroll_enabled() -> bool:
    """Whether CASASMART_DEV_ENROLL is set to a truthy value."""
    return os.environ.get(_DEV_ENROLL_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _async_setup_dev_enroll(
    hass: HomeAssistant, entry: CasaSmartConfigEntry, data_dir: Path
) -> None:
    """Keep the developer manifest's devices enrolled (development hubs only).

    Off unless CASASMART_DEV_ENROLL is set, so a stray dev_devices.json enrolls
    nobody. When on, provisions the manifest now and on every auth change,
    because the "Regenerate pairing code" button wipes devices without a
    reload. ensure_dev_devices never fires that event, so the listener can't
    trigger itself.
    """
    if not _dev_enroll_enabled():
        return
    _LOGGER.warning(
        "%s is set: dev device auto-enrollment is ACTIVE on this hub — never "
        "enable it on a hub that people rely on",
        _DEV_ENROLL_ENV,
    )

    auth = entry.runtime_data.auth

    async def _provision() -> None:
        await hass.async_add_executor_job(ensure_dev_devices, data_dir, auth)

    await _provision()

    @callback
    def _on_auth_changed(_event) -> None:
        entry.async_create_task(hass, _provision())

    entry.async_on_unload(hass.bus.async_listen(EVENT_AUTH_CHANGED, _on_auth_changed))


def _warn_keyless_speaker_provisioning(
    hub_config: JsonConfigStore, data_dir: Path
) -> None:
    """Warn in the log when keyless speaker provisioning is on."""
    if hub_config.get(KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY) is not True:
        return
    # A warning, since HA hides info logs by default and this setting exposes
    # the broker credentials to the local network.
    _LOGGER.warning(
        "Keyless speaker provisioning on: any device on the local network can "
        "fetch the speaker broker's username and password without the "
        "provisioning key. Speakers imaged with the key don't need it; set "
        '"%s": false in %s and restart Home Assistant to require the key.',
        KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY,
        data_dir / HUB_CONFIG_FILENAME,
    )


def _warn_lan_relay_ingress_on(hub_config: JsonConfigStore, port: int) -> None:
    """Log which LAN-gated features the trusted relay listener admits."""
    # Speaker provisioning uses the LAN gate only in keyless mode; otherwise
    # it needs the provisioning key from any address.
    gated = "pairing and recovery"
    if hub_config.get(KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY) is True:
        gated = "pairing, recovery and keyless speaker provisioning"
    # A warning, since HA hides info logs by default and operators need to
    # see that the LAN gate is relaxed.
    _LOGGER.warning(
        "LAN relay ingress on: connections on the hub TLS port %s count as "
        "LAN for %s. Publish that port to 127.0.0.1 only and reach it through "
        "the CasaSmart LAN relay (deploy/macos), which admits only LAN clients.",
        port,
        gated,
    )


def _configured_tls_port(hub_config: JsonConfigStore, data_dir: Path) -> int:
    """The tls_port from hub config, or the default when unset or unusable.

    The file is edited by hand, so anything but an integer from 1 to 65535 is
    logged and ignored instead of stopping setup or reaching mDNS, which
    refuses a bad port.
    """
    port = hub_config.get("tls_port")
    if port is None:
        return TLS_PORT_DEFAULT
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        _LOGGER.warning(
            "Ignoring invalid tls_port %r in %s (expected a whole number from 1 "
            "to 65535); using %s",
            port,
            data_dir / HUB_CONFIG_FILENAME,
            TLS_PORT_DEFAULT,
        )
        return TLS_PORT_DEFAULT
    return port


def _read_proc_version() -> str | None:
    """The kernel banner (identifies Docker Desktop's VM), or None off Linux."""
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


async def _async_start_tls(
    hass: HomeAssistant,
    entry: CasaSmartConfigEntry,
    data_dir: Path,
    hub_version: str,
) -> None:
    """Start the hub's HTTPS listener and the daily certificate check.

    A corrupt identity key raises ConfigEntryError: re-keying would break
    every paired phone's pin, so a person has to fix it. A port that won't
    bind is logged and retried by the daily check, and the views on HA's own
    port keep working. Also decides whether this listener counts as LAN
    ingress (lan_ingress.py) and logs what that means on this host.
    """
    runtime_data = entry.runtime_data
    try:
        material = await hass.async_add_executor_job(ensure_tls_material, data_dir)
    except IdentityError as err:
        # Retrying can't repair the key, so this is not ConfigEntryNotReady.
        raise ConfigEntryError(str(err)) from err

    port = _configured_tls_port(runtime_data.hub_config, data_dir)

    if runtime_data.hub_config.get(_RETIRED_EXTRA_LAN_CIDRS_KEY) is not None:
        # The setting could only add loopback to the LAN gate, which is where
        # a local tunnel's traffic comes from, so it is ignored.
        _LOGGER.warning(
            "%s in hub config is ignored since 2.3.0: private and link-local "
            "addresses already count as LAN. Remove it from %s.",
            _RETIRED_EXTRA_LAN_CIDRS_KEY,
            data_dir / HUB_CONFIG_FILENAME,
        )
    ingress_setting = runtime_data.hub_config.get(LAN_RELAY_INGRESS_CONFIG_KEY)
    if not is_recognized_lan_relay_ingress(ingress_setting):
        _LOGGER.warning(
            'Ignoring unrecognized %s %r in hub config (expected "on" or "off"); '
            'using "off"',
            LAN_RELAY_INGRESS_CONFIG_KEY,
            ingress_setting,
        )
    trusted_lan = resolve_lan_relay_ingress(ingress_setting)
    if trusted_lan:
        _warn_lan_relay_ingress_on(runtime_data.hub_config, port)
    elif needs_relay_ingress_hint(
        ingress_setting, await hass.async_add_executor_job(_read_proc_version)
    ):
        _LOGGER.warning(
            "Docker Desktop detected: the hub can't see phones' real addresses, "
            "only one Docker Desktop makes up and changes between restarts, so "
            "pairing and owner recovery will work or be refused unpredictably. "
            "If the TLS port is published to 127.0.0.1 only and reached "
            "through the CasaSmart LAN relay (deploy/macos), set "
            '"%s": "on" in %s and restart Home Assistant.',
            LAN_RELAY_INGRESS_CONFIG_KEY,
            data_dir / HUB_CONFIG_FILENAME,
        )
    else:
        _LOGGER.debug("LAN relay ingress off: the hub checks client addresses")

    server = CasaSmartTlsServer(hass, port, material, trusted_lan_ingress=trusted_lan)
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


async def _async_start_mdns(hass: HomeAssistant, entry: CasaSmartConfigEntry) -> None:
    """Advertise _casasmart._tcp so the app can find the hub on the LAN.

    The TXT id is the identity fingerprint the app pins, so a spoofed record
    can't get past TLS; this is why it runs after the listener starts. The
    record is re-published on a timer to follow a DHCP address change.
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
    runtime_data.mdns = advertiser
    await advertiser.async_start()

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
    """Start the push relay leg: dispatcher, relay registration, tank alerts.

    Needs the TLS identity (the relay knows the hub by its fingerprint) and a
    relay URL; without either, push stays off and the rest of the hub runs.
    The config flow's activation code goes to the registrar and is removed
    from the entry once the relay accepts the hub.
    """
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
    except (OSError, ConfigError) as err:
        # A read-only or full data directory; push is optional.
        _LOGGER.warning(
            "Push dispatcher skipped: the push-identity key could not be "
            "loaded or saved (%s)",
            err,
        )
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
    runtime_data.push_dispatcher = dispatcher
    dispatcher.async_start()

    async def _async_registration_ready() -> None:
        if (
            CONF_RELAY_ACTIVATION_CODE in entry.data
            or CONF_RELAY_ACTIVATION_REQUEST_ID in entry.data
        ):
            new_data = without_relay_activation(entry.data)
            hass.config_entries.async_update_entry(entry, data=new_data)
        persistent_notification.async_dismiss(hass, _NOTIFY_RELAY_ACTIVATION)
        persistent_notification.async_dismiss(hass, _NOTIFY_RELAY_CONFIGURATION)

    async def _async_registration_failed(reason: str) -> None:
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
    runtime_data.relay_registrar = registrar
    registrar.start(hass, entry)

    tank_monitor = TankPushMonitor(hass, tanks=runtime_data.tanks, notifier=dispatcher)
    runtime_data.tank_push_monitor = tank_monitor
    tank_monitor.async_start()


def _tunnel_options_snapshot(entry: CasaSmartConfigEntry) -> dict[str, Any]:
    """The tunnel-relevant slice of entry.options, for change detection."""
    return {
        CONF_CLOUDFLARE_DOMAIN: entry.options.get(CONF_CLOUDFLARE_DOMAIN),
        CONF_TUNNEL_ENABLED: entry.options.get(CONF_TUNNEL_ENABLED),
    }


async def _async_sync_tunnel_url(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """Derive hub_config["tunnel_url"] from the options domain, if one is set.

    The handshake and camera URLs read hub_config["tunnel_url"] per request,
    so no reload is needed. It stays advertised while the tunnel is switched
    off, because paired phones keep it as a fallback route. Without a domain,
    a URL set through the set_tunnel_url service is left alone.
    """
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    runtime_data = entry.runtime_data
    if domain:
        url = domain_to_tunnel_url(domain)
        if url is None:
            # Flow-validated domains can't get here; fail closed if one does.
            _LOGGER.warning(
                "Configured Cloudflare domain %r is unusable — not advertising it",
                domain,
            )
        elif runtime_data.hub_config.get(TUNNEL_URL_CONFIG_KEY) != url:
            # Write only on a change, so a normal boot costs no disk write.
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
    """React to a change of the entry's options or data (the update listener).

    Checked in order:

    1. An unusable relay update (no valid relay URL, or a malformed activation
       code) is rolled back to the relay in use, with a notification.
    2. A relay change or re-registration needs a complete activation code.
       Without one it is rolled back; with one, the relay leg is stopped and
       the entry reloaded.
    3. Otherwise the tunnel settings changed: derive (or stop advertising) the
       tunnel URL, then reconcile the cloudflared add-on in the background.
    """
    runtime_data = entry.runtime_data
    applied_relay = runtime_data.relay_config_applied
    configured_relay = normalize_relay_base_url(entry.options.get(CONF_PUSH_RELAY_URL))
    activation_raw = entry.data.get(CONF_RELAY_ACTIVATION_CODE)
    activation_code = activation_raw.strip() if isinstance(activation_raw, str) else ""
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
            persistent_notification.async_dismiss(hass, _NOTIFY_RELAY_CONFIGURATION)
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
            if derived is not None and hub_config.get(TUNNEL_URL_CONFIG_KEY) == derived:
                await hass.async_add_executor_job(
                    hub_config.delete, TUNNEL_URL_CONFIG_KEY
                )
                _LOGGER.info(
                    "Cloudflare domain cleared — no longer advertising %s",
                    derived,
                )
            # Without a domain the reconciler leaves the add-on alone, so undo
            # any boot=manual it set, as removing the entry does.
            if runtime_data.tunnel_control is not None:
                entry.async_create_background_task(
                    hass,
                    _async_restore_tunnel_boot(
                        runtime_data.tunnel_control, "Cloudflare domain cleared"
                    ),
                    name="casasmart-tunnel-restore-boot",
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
    """Bring the cloudflared add-on in line with the tunnel options.

    Each run compares the add-on's running state and boot mode with the
    options and closes the gap, so an add-on started by hand while the tunnel
    is off is stopped again, and one installed later is picked up. Runs at
    boot and after each options change. Failures are logged and shown as a
    notification, never raised.
    """
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    if not domain:
        # Leave cloudflared alone: it may be serving some other tunnel.
        return
    desired_on = bool(entry.options.get(CONF_TUNNEL_ENABLED, False))

    controller = entry.runtime_data.tunnel_control
    if controller is None:  # the entry is unloading; next setup reconciles
        return

    if not controller.available():
        # Container or Core install: the domain is still advertised, but
        # there is no add-on to control.
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
                # Remote access is now down; tell the owner why.
                persistent_notification.async_create(
                    hass,
                    f"The Cloudflare tunnel add-on ({slug}) was stopped and "
                    "set to manual start, as set in the CasaSmart integration "
                    "options. Phones can't reach the hub from outside the "
                    "home until you turn the tunnel back on there (gear "
                    "icon). Pairing doesn't need it: phones pair over the "
                    "local network.",
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
    # Success clears the notice from an earlier failed run.
    persistent_notification.async_dismiss(hass, _NOTIFY_TUNNEL_ERROR)


async def _async_tunnel_watchdog(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """Restart a cloudflared add-on that runs but has lost Cloudflare's edge.

    cloudflared can keep running while disconnected from the edge, which cuts
    remote access without any add-on error. This probes the public tunnel URL
    and restarts the add-on when Cloudflare reports the origin unreachable.
    Runs only while the tunnel is enabled and a URL is advertised; errors are
    logged, never raised.
    """
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
            # Starting a stopped add-on is the reconciler's job.
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
        # The edge is reachable again: clear the auto-recovery notice.
        persistent_notification.async_dismiss(hass, _NOTIFY_TUNNEL_EDGE_DOWN)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the casasmart.* services, once per Home Assistant run.

    The handlers look up the loaded entry at call time, so they outlive entry
    reloads and are never unregistered. factory_reset and set_tunnel_url go
    through Home Assistant's admin check: one unpairs every phone, the other
    changes where phones connect.
    """

    def _loaded_entry() -> ConfigEntry:
        """The loaded entry; a service called before setup fails cleanly."""
        entry = loaded_entry(hass)
        if entry is None:
            raise HomeAssistantError("CasaSmart hub is not loaded")
        return entry

    async def _handle_activate_scene(call) -> None:
        """Run a registry scene, as the app does (Energy Saving rules apply)."""
        runtime_data: CasaSmartRuntimeData = _loaded_entry().runtime_data
        scene_id = call.data.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id:
            raise HomeAssistantError("scene_id is required")
        registry = runtime_data.registry
        try:
            scene = await hass.async_add_executor_job(registry.get_scene, scene_id)
        except RegistryError as err:
            raise HomeAssistantError(str(err)) from err
        energy = getattr(runtime_data, "energy", None)
        if (
            energy is not None
            and energy.active_level is not None
            and not scene.get("works_during_energy_saving", False)
        ):
            raise HomeAssistantError("Scene is disabled while Energy Saving is active")
        result = await async_execute_registry_scene(hass, scene)
        if not result["ok"]:
            failed = [item["entity_id"] for item in result["results"] if not item["ok"]]
            raise HomeAssistantError(
                f"Scene {scene_id} failed for: {', '.join(failed)}"
            )

    async def _handle_set_tunnel_url(call) -> None:
        """Store the advertised tunnel URL; a bare origin also sets the domain."""
        entry = _loaded_entry()
        runtime_data: CasaSmartRuntimeData = entry.runtime_data

        url = normalize_tunnel_url(call.data.get("url"))
        if url is None:
            raise HomeAssistantError(
                "Invalid tunnel URL — must be a plain https origin "
                "(no userinfo/query/fragment)"
            )

        try:
            await hass.async_add_executor_job(
                runtime_data.hub_config.set, TUNNEL_URL_CONFIG_KEY, url
            )
        except ConfigError as err:
            raise HomeAssistantError(f"Could not save the tunnel URL: {err}") from err
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

    async def _handle_configure_hq_notifications(call) -> None:
        """Trust one HQ signing key and optional sender name (HA admins only).

        Each call replaces the whole trust and clears the HQ ingress state
        kept under the previous key (replay nonces and the receipt log).
        """
        user_id = call.context.user_id
        user = await hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            raise HomeAssistantError(
                "CasaSmart HQ notification trust requires a Home Assistant admin"
            )
        entry = _loaded_entry()
        try:
            public_key, fingerprint = normalize_public_key(call.data.get("public_key"))
        except HqNotificationError as err:
            raise HomeAssistantError("A valid Ed25519 public key is required") from err
        try:
            sender_name = normalize_sender_name(call.data.get("sender_name"))
        except HqNotificationError as err:
            raise HomeAssistantError(
                "The sender name must be a single line of at most "
                f"{HQ_SENDER_NAME_MAX_LENGTH} characters"
            ) from err
        runtime_data: CasaSmartRuntimeData = entry.runtime_data

        def _install_key() -> None:
            runtime_data.hub_config.set(
                HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY, public_key
            )
            # Each call defines the whole trust: no name means the default title.
            if sender_name is None:
                runtime_data.hub_config.delete(HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY)
            else:
                runtime_data.hub_config.set(
                    HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY, sender_name
                )
            runtime_data.storage.table("hq_notifications").clear()

        try:
            await hass.async_add_executor_job(_install_key)
        except (StorageError, sqlite3.Error) as err:
            raise HomeAssistantError(
                f"Could not save the HQ notification trust: {err}"
            ) from err
        _LOGGER.info(
            "HQ notification trust configured (fingerprint=%s, sender name=%s)",
            fingerprint,
            sender_name or "default",
        )

    async def _handle_factory_reset(call) -> None:
        """Wipe the app layer (FACTORY_RESET_TABLES) and reload the entry.

        Energy Saving is stopped first so the automations it disabled come
        back; if any can't, the reset stops before wiping anything. The tables
        are wiped in one transaction. The owner code hashes are deleted next,
        so the reload mints a new pairing code and recovery card; if that
        fails, the reload still runs, and running the reset again finishes it.
        """
        entry = _loaded_entry()
        runtime_data: CasaSmartRuntimeData = entry.runtime_data

        if runtime_data.energy_controller is not None:
            await runtime_data.energy_controller.async_deactivate(actor="factory_reset")
        pending_automations = await hass.async_add_executor_job(
            runtime_data.energy_flags.disabled_automations
        )
        if pending_automations:
            raise HomeAssistantError(
                "Factory reset paused because Energy Saving could not restore: "
                + ", ".join(pending_automations)
            )

        def _wipe_tables() -> None:
            with runtime_data.storage.transaction():
                for table in FACTORY_RESET_TABLES:
                    runtime_data.storage.table(table).clear()
                runtime_data.storage.energy_events().clear()

        def _forget_codes() -> None:
            runtime_data.hub_config.delete("registry_imported")
            runtime_data.hub_config.delete(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
            runtime_data.hub_config.delete(RECOVERY_CODE_HASH_CONFIG_KEY)
            runtime_data.hub_config.delete(HQ_NOTIFICATION_PUBLIC_KEY_CONFIG_KEY)
            runtime_data.hub_config.delete(HQ_NOTIFICATION_SENDER_NAME_CONFIG_KEY)

        try:
            await hass.async_add_executor_job(_wipe_tables)
        except (StorageError, sqlite3.Error) as err:
            raise HomeAssistantError(f"Factory reset could not finish: {err}") from err
        # Every phone is unpaired now; don't leave closing them to the reload.
        await async_close_connections(hass)
        try:
            await hass.async_add_executor_job(_forget_codes)
        except ConfigError as err:
            # The in-memory caches no longer match the wiped tables.
            await hass.config_entries.async_reload(entry.entry_id)
            raise HomeAssistantError(f"Factory reset could not finish: {err}") from err
        _LOGGER.warning(
            "CasaSmart factory reset (full blank): wiped devices, pairing, "
            "recovery, favorites, scenes, settings, push, alarm log/state, "
            "audio config + speakers, Energy Saving data, and the registry "
            "org layer (floors/rooms/tags/"
            "assignments/grouping) — re-seeding from HA on reload; printed "
            "codes rotated"
        )

        await hass.config_entries.async_reload(entry.entry_id)

    admin_handlers = {
        "factory_reset": (_handle_factory_reset, vol.Schema({})),
        "set_tunnel_url": (
            _handle_set_tunnel_url,
            vol.Schema({vol.Required("url"): cv.string}),
        ),
    }
    for service, (handler, schema) in admin_handlers.items():
        if not hass.services.has_service(DOMAIN, service):
            async_register_admin_service(hass, DOMAIN, service, handler, schema)
    handlers = {
        "activate_scene": _handle_activate_scene,
        "configure_hq_notifications": _handle_configure_hq_notifications,
    }
    for service, handler in handlers.items():
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler)


async def async_unload_entry(hass: HomeAssistant, entry: CasaSmartConfigEntry) -> bool:
    """Unload the platforms, then stop every runtime and close storage.

    If a platform fails to unload, its entities still use the engines, so
    nothing is stopped and False is returned; the stop listener still closes
    storage at shutdown.
    """
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        _LOGGER.error("CasaSmart Hub platforms failed to unload; hub left running")
        return False
    await _async_stop_hub(hass, entry.runtime_data)
    _LOGGER.info("CasaSmart Hub storage closed")
    return True


async def _async_stop_hub(
    hass: HomeAssistant, runtime_data: CasaSmartRuntimeData
) -> None:
    """Stop every runtime that started, then close storage.

    Shared by unload and a failed setup. WebSockets close first, so the TLS
    listener doesn't wait on them, and storage closes last.
    """
    await async_close_connections(hass)
    if runtime_data.suggestions is not None:
        runtime_data.suggestions.stop()
    if runtime_data.energy_controller is not None:
        await runtime_data.energy_controller.async_stop()
    if runtime_data.alarm_adapter is not None:
        runtime_data.alarm_adapter.async_stop()
    if runtime_data.tank_push_monitor is not None:
        runtime_data.tank_push_monitor.async_stop()
    if runtime_data.relay_registrar is not None:
        runtime_data.relay_registrar.stop()
    if runtime_data.push_dispatcher is not None:
        runtime_data.push_dispatcher.async_stop()
    if runtime_data.athan_scheduler is not None:
        await runtime_data.athan_scheduler.async_stop()
    if runtime_data.audio_adapter is not None:
        await runtime_data.audio_adapter.async_stop()
    if runtime_data.mdns is not None:
        await runtime_data.mdns.async_stop()
    if runtime_data.tls is not None:
        await runtime_data.tls.async_stop()
    await hass.async_add_executor_job(runtime_data.storage.close)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give cloudflared its auto-boot back when the entry is deleted.

    runtime_data is already gone here, so a fresh controller is built.
    Without a domain there is nothing to undo: clearing it already restored
    the boot mode.
    """
    if not entry.options.get(CONF_CLOUDFLARE_DOMAIN):
        return
    await _async_restore_tunnel_boot(CloudflaredController(hass), "CasaSmart removed")


async def _async_restore_tunnel_boot(
    controller: CloudflaredController, reason: str
) -> None:
    """Set cloudflared back to boot=auto once CasaSmart stops managing it.

    Used when the domain is cleared or the entry is removed: a boot=manual
    left by a disabled tunnel must not strand remote access for good. The
    add-on is not started, since giving up control is no reason to open
    remote access now. Best effort: errors are logged and dropped.
    """
    if not controller.available():
        return
    try:
        slug = await controller.async_discover()
        if slug is not None:
            await controller.async_restore_boot_auto(slug)
            _LOGGER.info(
                "%s — cloudflared add-on %s restored to boot=auto", reason, slug
            )
    except TunnelControlError as err:
        _LOGGER.warning("Could not restore cloudflared boot mode (%s): %s", reason, err)
