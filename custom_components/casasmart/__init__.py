"""CasaSmart Hub integration (Track B — B1.3: storage + REST skeleton).

Setup opens the B1.1 storage layer (SQLite+WAL + JSON config store) under
<ha-config>/casasmart/, parks it in runtime data, and registers the B1.3
REST views (version handshake + health probe). Entities and the entity
bridge arrive in B1.4+. B10 adds the hub's permanent TLS identity and the
dedicated HTTPS listener. Unload stops the listener and closes storage.
"""

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
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
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
    CONF_TUNNEL_ENABLED,
    DATA_DIR_NAME,
    DB_FILENAME,
    DOMAIN,
    EVENT_AUTH_CHANGED,
    HUB_CONFIG_FILENAME,
    HUB_NAME_CONFIG_KEY,
    MDNS_REFRESH_INTERVAL_MINUTES,
    PUSH_RELAY_PUSH_PATH,
    PUSH_RELAY_URL_CONFIG_KEY,
    PUSH_RELAY_URL_DEFAULT,
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
from .alarm import AlarmEngine
from .alarm_adapter import AlarmAdapter
from .audio import AudioEngine
from .audio_adapter import AudioAdapter
from .athan_scheduler import AthanScheduler
from .recovery import RecoveryManager, hash_code as recovery_hash_code
from .registry import RegistryEngine, RegistryError
from .registry_api import async_execute_registry_scene
from .storage import HubStorage, JsonConfigStore, StorageError
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

# Persistent-notification ids for the tunnel reconciler — fixed so repeats
# replace the previous notice instead of stacking.
_NOTIFY_TUNNEL_UNAVAILABLE = f"{DOMAIN}_tunnel_control_unavailable"
_NOTIFY_TUNNEL_AUTO_DISABLED = f"{DOMAIN}_tunnel_auto_disabled"
_NOTIFY_TUNNEL_ERROR = f"{DOMAIN}_tunnel_control_error"
_NOTIFY_TUNNEL_EDGE_DOWN = f"{DOMAIN}_tunnel_edge_down"

# HA entity platforms the hub exposes:
#  - ALARM_CONTROL_PANEL (B13): the panel mirroring the hub-authoritative
#    AlarmEngine.
#  - BUTTON: the owner's "regenerate pairing code" access-management control.
#  - SENSOR: one per enrolled device — role as state, paired/last-seen detail.
# Everything else is REST/WS, not HA entities.
PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BUTTON,
    Platform.SENSOR,
]

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
    # B8 push-token store: FCM tokens for encrypted relay dispatch.
    push: PushTokenStore
    # B13 hub-side alarm state machine (push leg stubbed until B8).
    alarm: AlarmEngine
    # B14 hub-side audio engine: broker/PA/athan config + speaker registry +
    # live-status mirror. The single source of truth the phone reads over REST.
    audio: AudioEngine
    # B13 alarm HA glue: drives the engine off state_changed + the entry-delay
    # timer. None only if setup failed before it was started.
    alarm_adapter: AlarmAdapter | None = None
    # B14 audio MQTT glue: the hub's only broker connection. None if setup
    # failed before it started, or while audio is unprovisioned (still set,
    # just inert — the object exists so the API can publish through it).
    audio_adapter: AudioAdapter | None = None
    # Hub-native athan scheduler: computes prayer times + fires broadcast plays
    # via the audio adapter. None only if setup failed before it was started.
    athan_scheduler: AthanScheduler | None = None
    # B8 push dispatcher: alarm/lock events -> signed relay pushes. None if the
    # TLS identity (its hub-id source) or the push key was unavailable at setup.
    push_dispatcher: PushDispatcher | None = None
    # B8 Piece 4b tank monitor: timer-driven low-water + offline pushes. Shares
    # the dispatcher's relay path, so None whenever push_dispatcher is None.
    tank_push_monitor: TankPushMonitor | None = None
    # B10 HTTPS listener; None only if the identity/cert layer failed setup.
    tls: CasaSmartTlsServer | None = None
    # B6 mDNS advertiser; None if TLS identity (its hub-id source) is absent
    # or zeroconf wasn't ready — discovery degrades, the hub stays reachable.
    mdns: MdnsAdvertiser | None = None
    # Cloudflare tunnel management: the Supervisor-coupled controller (its
    # slug cache lives for the entry's lifetime). None until setup wires it.
    tunnel_control: CloudflaredController | None = None
    # Snapshot of the tunnel options last applied to hub_config, so the
    # update listener can retire a domain-DERIVED tunnel_url when the domain
    # is cleared — without ever touching a service-set URL (B7 installer path).
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
    str | None,
    str | None,
]:
    """Open storage + config + auth + pairing + recovery + registry +
    tanks + user settings + audio (blocking — executor only)."""
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
    recovery = RecoveryManager(storage.table("recovery_codes"), auth.has_admin)
    # PERMANENT printed codes (B2/B3): the admin "acquire" code and the owner
    # recovery code are minted ONCE at first provisioning; their hashes are
    # persisted in hub_config so the printed sticker + metal card survive a
    # factory reset (the storage tables are wiped, this JSON file is not) and are
    # re-installed from those hashes on every boot. Plaintext exists exactly
    # once — surfaced only the first time each is minted, for printing.
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
        # Minted at first provisioning regardless of claim state, so the owner's
        # metal card can be engraved alongside the admin sticker. Inert until an
        # admin exists (replace_admin needs one); permanent + reusable thereafter.
        recovery_code = recovery.mint_permanent()
        hub_config.set(
            RECOVERY_CODE_HASH_CONFIG_KEY, recovery_hash_code(recovery_code)
        )
    # Speaker-provisioning secret — baked into each site's Pi image (platform.json)
    # and presented on GET /audio/provision so a speaker can fetch broker creds
    # from any source (incl. a Docker-NAT'd hub whose LAN check misfires) without
    # exposing them off-network. Minted once; survives factory reset via hub_config.
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
    registry.warm_up()  # room/name mirrors loaded — event-loop reads stay pure CPU
    tanks = TankEngine(
        storage.table("tank_devices"),
        storage.tank_readings(),
    )
    user_settings = UserSettingsEngine(storage.table("user_settings"))
    push = PushTokenStore(storage.table("push_tokens"))
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
    # B14 audio: broker/PA/athan config + enrolled-speaker registry. The live
    # per-speaker status mirror is NOT loaded here — it is rebuilt from the
    # broker's retained topics when the adapter connects.
    audio = AudioEngine(
        storage.table("audio_config"),
        storage.table("audio_speakers"),
    )
    audio.warm_up()  # config + registry loaded — event-loop reads stay pure CPU
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
            push,
            alarm,
            audio,
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
    )

    # Cloudflare domain (entry options) -> advertised tunnel_url (hub_config),
    # before the TLS/handshake layer comes up so the first handshake already
    # carries it. No options domain -> hub_config untouched (B7 service path).
    await _async_sync_tunnel_url(hass, entry)

    await _async_import_registry(hass, hub_config, registry)

    # DEV-ONLY auto-enrollment: re-provision trusted dev keys (mint_token.py et
    # al.) so the dev tooling survives every factory reset with zero manual
    # steps. Inert on client hubs — no dev_devices.json manifest, no effect.
    # Runs now (covers boot + the service-reset reload) and on every
    # EVENT_AUTH_CHANGED (covers the button reset, which wipes in place without
    # a reload). Off unless the CASASMART_DEV_ENROLL env flag is set, so a
    # stray/deployed manifest never auto-creates a shadow user. See dev_enroll.py.
    await _async_setup_dev_enroll(hass, entry, data_dir)

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
        # Plaintext exists exactly once — the operator engraves the metal card
        # from this notification (B3 backup tier).
        notify_recovery_code(hass, recovery_code)

    _async_register_services(hass)

    # Hub version = the integration's manifest version (single source of truth).
    integration = await async_get_integration(hass, DOMAIN)
    hub_version = str(integration.version) if integration.version else "0.0.0"
    async_register_views(hass, hub_version=hub_version)

    await _async_start_tls(hass, entry, data_dir, hub_version)
    await _async_start_mdns(hass, entry)
    await _async_start_push(hass, entry, data_dir)

    # B13: wire the alarm engine to live HA events (sensor edges + the
    # entry-delay timer). Pure event subscription — no blocking work.
    alarm_adapter = AlarmAdapter(hass, alarm)
    alarm_adapter.async_start()
    entry.runtime_data.alarm_adapter = alarm_adapter

    # B14: bring up the hub's single MQTT client. Inert (logged) when no broker
    # is provisioned yet — never aborts setup. connect_async/loop_start don't
    # block the loop; the network thread owns connect + reconnect.
    audio_adapter = AudioAdapter(hass, audio)
    await audio_adapter.async_start()
    entry.runtime_data.audio_adapter = audio_adapter

    # Hub-native athan scheduler: computes prayer times locally and fires them
    # through the same adapter (broadcast play, priority athan). Replaces the
    # old external casaos-athan-scheduler daemon — ships in-process with the hub.
    athan_scheduler = AthanScheduler(hass, audio, audio_adapter)
    await athan_scheduler.async_start()
    entry.runtime_data.athan_scheduler = athan_scheduler

    # B13: expose the alarm panel as a native HA entity (runtime_data — its
    # engine source — is already set above, so the platform can read it).
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Cloudflare tunnel desired-state enforcement. The options listener does
    # sync + reconcile on every change (flow save / service mirror); the boot
    # reconcile runs as a BACKGROUND task so a wedged Supervisor can never
    # block or fail hub setup (same degrade-gracefully doctrine as mDNS/push).
    entry.runtime_data.tunnel_control = CloudflaredController(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_create_background_task(
        hass,
        _async_reconcile_tunnel(hass, entry),
        name="casasmart-tunnel-reconcile",
    )
    # Edge-liveness watchdog (Phase 9): the reconcile above only enforces the
    # add-on is running; this periodic probe confirms cloudflared is actually
    # connected to Cloudflare's edge and restarts it if not, so a flapped tunnel
    # self-heals instead of showing "offline" until a human intervenes.
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


# DEV-ONLY auto-enrollment opt-in. The ``dev_devices.json`` manifest alone no
# longer auto-creates a shadow sub-admin user: that surprised owners on dev
# hubs because the seam re-provisions on every EVENT_AUTH_CHANGED, so the device
# reappeared after every delete / factory reset. Enrollment now ALSO requires
# this env flag, so the seam is off by default even when a manifest is present —
# defence-in-depth on top of "no manifest ships to clients", and a clean
# single-admin hub unless a developer explicitly opts in
# (``CASASMART_DEV_ENROLL=1``).
_DEV_ENROLL_ENV = "CASASMART_DEV_ENROLL"


def _dev_enroll_enabled() -> bool:
    """True only when the dev auto-enroll env flag is explicitly truthy."""
    return os.environ.get(_DEV_ENROLL_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _async_setup_dev_enroll(
    hass: HomeAssistant, entry: CasaSmartConfigEntry, data_dir: Path
) -> None:
    """DEV-ONLY: keep the trusted dev keys enrolled across factory resets.

    Gated behind [_DEV_ENROLL_ENV] (default OFF): without the opt-in the dev
    manifest never auto-enrolls anyone, so dev/test hubs — and any hub that
    accidentally ships a manifest — stay clean single-admin by default.

    When enabled, provisions the ``dev_devices.json`` manifest now (boot /
    service-reset reload) and re-runs it on every ``EVENT_AUTH_CHANGED`` so the
    BUTTON reset — which wipes the auth tables in place without reloading the
    entry — also re-provisions. A no-op on any hub without the manifest (every
    client hub). The seam is idempotent and never fires ``EVENT_AUTH_CHANGED``
    itself, so the listener can't feed itself. The listener is torn down with
    the entry via ``async_on_unload``.
    """
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


async def _async_start_push(
    hass: HomeAssistant, entry: CasaSmartConfigEntry, data_dir: Path
) -> None:
    """B8: wire alarm/lock events to signed relay pushes.

    The dispatcher signs every batch with the hub's Ed25519 push-identity key
    and authenticates to the relay as the hub's permanent identity fingerprint
    — the same id the app pins and the relay knows out-of-band. It therefore
    needs the TLS identity for its hub-id, so it runs after TLS; if that layer
    failed (``runtime_data.tls is None``) there is nothing stable to sign as and
    push is skipped — the rest of the hub stays up. A corrupt push key degrades
    the same way (logged, never fatal): notifications are one feature, not the
    hub.
    """
    runtime_data = entry.runtime_data
    if runtime_data.tls is None:
        _LOGGER.info("Push dispatcher skipped — TLS identity unavailable")
        return

    try:
        signer = await hass.async_add_executor_job(
            ensure_push_identity, data_dir, runtime_data.hub_config
        )
    except PushIdentityError:
        _LOGGER.exception("Push dispatcher skipped — push-identity key unusable")
        return

    relay_base = runtime_data.hub_config.get(PUSH_RELAY_URL_CONFIG_KEY)
    if not isinstance(relay_base, str) or not relay_base.strip():
        relay_base = PUSH_RELAY_URL_DEFAULT
    relay_url = relay_base.rstrip("/") + PUSH_RELAY_PUSH_PATH

    dispatcher = PushDispatcher(
        hass,
        push_store=runtime_data.push,
        signer=signer,
        hub_id=runtime_data.tls.material.identity_fingerprint,
        relay_url=relay_url,
        session=async_get_clientsession(hass),
    )
    dispatcher.async_start()
    runtime_data.push_dispatcher = dispatcher

    # B8 Piece 4b: the tank monitor pushes through the dispatcher, so it only
    # comes up once push is up (no notifier, no monitor). Timer-driven — pure
    # scheduling, no blocking work.
    tank_monitor = TankPushMonitor(
        hass, tanks=runtime_data.tanks, notifier=dispatcher
    )
    tank_monitor.async_start()
    runtime_data.tank_push_monitor = tank_monitor


def _tunnel_options_snapshot(entry: CasaSmartConfigEntry) -> dict[str, Any]:
    """The tunnel-relevant slice of entry.options, for change detection."""
    return {
        CONF_CLOUDFLARE_DOMAIN: entry.options.get(CONF_CLOUDFLARE_DOMAIN),
        CONF_TUNNEL_ENABLED: entry.options.get(CONF_TUNNEL_ENABLED),
    }


async def _async_sync_tunnel_url(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """Derive hub_config["tunnel_url"] from the options domain (if set).

    Options are the user surface; ``hub_config["tunnel_url"]`` stays the
    canonical *advertised* URL — the handshake (api.py) and camera URLs
    (camera_api.py) read it live per request, so no reload is ever needed.
    The URL keeps being advertised even while the tunnel is toggled OFF:
    phones capture the remote path at pairing for when it returns (their
    fallback chain tolerates a dead URL), and pairing is LAN-only anyway.

    Without an options domain this is a no-op — a service-set URL from the
    B7 installer path is never touched.
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
            # JsonConfigStore writes are blocking (executor), and skipped
            # when the value is current so boot never costs a disk write.
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
    """Entry options changed (options flow save, or the service mirror).

    Syncs the advertised URL, retires it when the domain was cleared, and
    schedules a reconcile. Deliberately NO entry reload: nothing caches
    ``tunnel_url`` (both consumers read it live — verified), and a reload
    would needlessly drop live app WS connections and restart TLS/MQTT.
    """
    runtime_data = entry.runtime_data
    previous = runtime_data.tunnel_options_applied or {}
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    previous_domain = previous.get(CONF_CLOUDFLARE_DOMAIN)

    if domain:
        await _async_sync_tunnel_url(hass, entry)
    else:
        if previous_domain:
            # Domain cleared via the options flow. Retire the advertised URL
            # only if it is still the one WE derived — a URL set through the
            # casasmart.set_tunnel_url service is not ours to delete.
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
    """Enforce the desired tunnel state (options) on the cloudflared add-on.

    Desired-state model, not imperative start/stop: every run compares what
    the add-on IS (running? boot mode?) against what the options SAY and
    closes the gap — so a manually-started add-on while the toggle is OFF
    gets stopped again on the next run, and installing the add-on after the
    fact "just works". Runs at boot and after every options change; every
    failure notifies + logs and never raises — the tunnel is one feature,
    not the hub.
    """
    domain = entry.options.get(CONF_CLOUDFLARE_DOMAIN)
    if not domain:
        # No domain -> fully inert. Never touch an add-on we weren't pointed
        # at — an installed cloudflared may be tunneling something else.
        return
    desired_on = bool(entry.options.get(CONF_TUNNEL_ENABLED, False))

    controller = entry.runtime_data.tunnel_control
    if controller is None:  # unloading race — next setup reconciles
        return

    if not controller.available():
        # Container/Core install (like the dev hub): domain storage and
        # handshake advertising still work; only add-on control is inert.
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
                # We just took a live tunnel down — say why, loudly.
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
    # Reconciled clean — retire any stale failure notice from earlier runs.
    persistent_notification.async_dismiss(hass, _NOTIFY_TUNNEL_ERROR)


async def _async_tunnel_watchdog(
    hass: HomeAssistant, entry: CasaSmartConfigEntry
) -> None:
    """Periodic edge-liveness check (Phase 9): heal a running-but-offline tunnel.

    The reconciler only knows whether the add-on is *running*; cloudflared can
    be running yet disconnected from Cloudflare's edge (network flap, edge drop)
    — remote access goes dark with no add-on error. This probes the public
    tunnel URL through the edge and restarts the add-on when Cloudflare reports
    the origin unreachable. Only fires while the tunnel is meant to be ON and a
    URL is advertised; never raises — the tunnel is one feature, not the hub.
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
            # A stopped add-on is the reconciler's job (it starts it); the
            # watchdog only heals a tunnel that IS running but edge-dead.
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
        # Edge is healthy — clear any prior auto-recovery notice.
        persistent_notification.async_dismiss(hass, _NOTIFY_TUNNEL_EDGE_DOWN)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register hub services (idempotent across entry reloads).

    ``casasmart.factory_reset`` is the B3 nuclear tier: the operator, over
    Tailscale -> HA or on-site, wipes the CasaSmart APP layer only —
    paired phones, pairing codes, recovery code. HA devices, automations
    and the Zigbee mesh are untouched. Reachable only through HA itself
    (admin login or trusted CLI), never through the CasaSmart API — a
    stolen app token can't factory-reset the hub.
    """
    if all(
        hass.services.has_service(DOMAIN, service)
        for service in ("factory_reset", "set_tunnel_url", "activate_scene")
    ):
        return

    async def _handle_activate_scene(call) -> None:
        """Run a CasaSmart registry scene from an HA automation."""
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
        """Persist the hub's public tunnel URL into hub_config (plan B7).

        The installer dashboard calls this over the HA API at onboarding,
        after creating the Cloudflare tunnel + DNS, so the version handshake
        re-advertises the remote path to the app AT PAIRING (no manual URL
        entry on the phone). ``normalize_tunnel_url`` fails closed — an
        invalid/non-https URL is rejected here rather than handed to phones.

        No entry reload (behavior change): both consumers (api.py handshake,
        camera_api.py) read hub_config live per request — verified — so the
        URL is advertised on the very next handshake, and a reload would
        needlessly drop live app WS connections. A bare-origin URL is also
        mirrored into the entry options so the options flow shows reality;
        a NEW domain seeds tunnel_enabled=False (same contract as setup:
        fresh tunnel domains start disabled for LAN-only pairing).
        """
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

        # Keep the snapshot honest: this URL is now service-set, so a later
        # options-flow domain clear must not delete it (unless the mirror
        # below re-derives it from an options domain).
        runtime_data.tunnel_options_applied = _tunnel_options_snapshot(entry)

        domain = normalize_cloudflare_domain(url)
        if domain is not None and entry.options.get(CONF_CLOUDFLARE_DOMAIN) != domain:
            new_options = dict(entry.options)
            new_options[CONF_CLOUDFLARE_DOMAIN] = domain
            new_options.setdefault(CONF_TUNNEL_ENABLED, False)
            # Fires the update listener -> sync (no-op, same URL) + reconcile.
            hass.config_entries.async_update_entry(entry, options=new_options)
        elif domain is None:
            # Path-prefixed / ported URL — can't be expressed as a domain;
            # options left alone, the URL above still advertises.
            _LOGGER.debug(
                "Tunnel URL %s is not a bare origin — not mirrored to options",
                url,
            )

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
            # phones. Floors/rooms (the physical layout) are HOUSE data and
            # survive an ownership transfer.
            runtime_data.storage.table("registry_favorites").clear()
            # A scene is owner-AUTHORED content (named, encoding the owner's
            # brightness/temperature/curtain choices) — it belongs with the
            # personal data, NOT the bricks-and-mortar room list. A new owner
            # must not inherit it, and it can reference entities that no longer
            # exist after the reset (re-audit follow-up).
            runtime_data.storage.table("registry_scenes").clear()
            # MB-2: per-user settings are phone-layer too, same fate.
            # Tank devices/readings are HOUSE data (the Shelly keeps
            # posting through an ownership transfer) and survive.
            runtime_data.storage.table("user_settings").clear()
            # B8: push tokens are phone-layer data — wipe on reset.
            runtime_data.storage.table("push_tokens").clear()
            # Phase 3: the previous owner's alarm LOG and armed STATE must not
            # carry to a new owner — clear both (the reload re-warms the engine
            # to disarmed). Zones + settings are HOUSE config and survive.
            runtime_data.storage.table("alarm_history").clear()
            runtime_data.storage.table("alarm_state").clear()
            # Phase 7: the previous owner's AUDIO data is personal/credential —
            # athan GPS coordinates, named speakers, the broker password + PA
            # api-key — and must NOT survive a handover. Clear both audio tables;
            # the new owner re-provisions speakers + reconfigures the broker.
            runtime_data.storage.table("audio_config").clear()
            runtime_data.storage.table("audio_speakers").clear()
            # Full-blank reset (user choice): wipe the ENTIRE CasaSmart
            # organizational layer — floors, rooms, per-entity assignments,
            # grouped-device structure — and clear the one-time import flag so
            # the entry reload RE-SEEDS floors/rooms/assignments from HA's area
            # registry (the canonical source — _async_import_registry). The
            # owner's CasaSmart edits (renamed rooms, manual moves, gang
            # grouping) go; the home re-derives clean defaults from HA. This
            # supersedes the old scrub-labels-only ownership-transfer behavior.
            runtime_data.storage.table("registry_floors").clear()
            runtime_data.storage.table("registry_rooms").clear()
            runtime_data.storage.table("registry_devices").clear()
            runtime_data.storage.table("registry_user_devices").clear()
            runtime_data.hub_config.delete("registry_imported")
            # Phase 3: rotate the printed credentials. Deleting the persisted
            # hashes makes the reload re-mint AND re-surface a fresh admin
            # sticker code + owner recovery code, so the previous owner's
            # printed card/sticker can no longer re-claim the hub.
            runtime_data.hub_config.delete(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
            runtime_data.hub_config.delete(RECOVERY_CODE_HASH_CONFIG_KEY)

        await hass.async_add_executor_job(_wipe)
        _LOGGER.warning(
            "CasaSmart factory reset (full blank): wiped devices, pairing, "
            "recovery, favorites, scenes, settings, push, alarm log/state, "
            "audio config + speakers, and the registry org layer (floors/rooms/"
            "assignments/grouping) — re-seeding from HA on reload; printed "
            "codes rotated"
        )
        # Reload rebuilds the engine caches from the now-empty tables and
        # re-mints the bootstrap pairing code for re-onboarding.
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
    """Unload a config entry, stopping the mDNS/TLS listeners and storage."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if entry.runtime_data.alarm_adapter is not None:
        entry.runtime_data.alarm_adapter.async_stop()
    if entry.runtime_data.tank_push_monitor is not None:
        entry.runtime_data.tank_push_monitor.async_stop()
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
    """Entry deleted: best-effort restore of cloudflared to boot=auto.

    The reconciler may have parked the add-on at boot=manual (tunnel
    disabled). Removing the integration must not permanently strand the
    client's remote access, so hand auto-boot back to the Supervisor —
    without starting the add-on (removal is not consent to open remote
    access right now). Best effort by design: no Supervisor, no add-on, or
    a Supervisor error is logged and dropped. runtime_data is already gone
    here, so a fresh controller is built.
    """
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
