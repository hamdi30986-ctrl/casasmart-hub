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
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

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
    RECOVERY_CODE_HASH_CONFIG_KEY,
    TLS_CERT_CHECK_INTERVAL_HOURS,
    TLS_PORT_DEFAULT,
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
from .recovery import RecoveryManager, hash_code as recovery_hash_code
from .registry import RegistryEngine
from .storage import HubStorage, JsonConfigStore, StorageError
from .tank import TankEngine
from .tls import CasaSmartTlsServer, IdentityError, ensure_tls_material
from .tunnel import TUNNEL_URL_CONFIG_KEY, normalize_tunnel_url
from .user_settings import UserSettingsEngine

_LOGGER = logging.getLogger(__name__)

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

    async def _handle_set_tunnel_url(call) -> None:
        """Persist the hub's public tunnel URL into hub_config (plan B7).

        The installer dashboard calls this over the HA API at onboarding,
        after creating the Cloudflare tunnel + DNS, so the version handshake
        re-advertises the remote path to the app AT PAIRING (no manual URL
        entry on the phone). ``normalize_tunnel_url`` fails closed — an
        invalid/non-https URL is rejected here rather than handed to phones.
        Reloading the entry rebuilds the handshake with the stored URL.
        """
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("CasaSmart hub is not loaded")
        runtime_data: CasaSmartRuntimeData = entries[0].runtime_data

        url = normalize_tunnel_url(call.data.get("url"))
        if url is None:
            raise HomeAssistantError(
                "Invalid tunnel URL — must be a plain https origin "
                "(no userinfo/query/fragment)"
            )

        await hass.async_add_executor_job(
            runtime_data.hub_config.set, TUNNEL_URL_CONFIG_KEY, url
        )
        _LOGGER.info("CasaSmart tunnel URL set; reloading to re-advertise it")
        await hass.config_entries.async_reload(entries[0].entry_id)

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
            # Phase 3: grouped device STRUCTURE is HOUSE data (gang typing /
            # wiring) and survives, but the owner's LABELS are scrubbed —
            # device custom names/icons, gang names, per-entity display names.
            runtime_data.registry.scrub_owner_labels()
            # Phase 3: rotate the printed credentials. Deleting the persisted
            # hashes makes the reload re-mint AND re-surface a fresh admin
            # sticker code + owner recovery code, so the previous owner's
            # printed card/sticker can no longer re-claim the hub.
            runtime_data.hub_config.delete(BOOTSTRAP_CODE_HASH_CONFIG_KEY)
            runtime_data.hub_config.delete(RECOVERY_CODE_HASH_CONFIG_KEY)

        await hass.async_add_executor_job(_wipe)
        _LOGGER.warning(
            "CasaSmart factory reset: app layer wiped (devices, pairing, "
            "recovery, favorites, scenes, settings, push, alarm log/state, "
            "audio config + speakers), owner device labels scrubbed, printed "
            "codes rotated"
        )
        # Reload rebuilds the engine caches from the now-empty tables and
        # re-mints the bootstrap pairing code for re-onboarding.
        await hass.config_entries.async_reload(entries[0].entry_id)

    hass.services.async_register(DOMAIN, "factory_reset", _handle_factory_reset)
    hass.services.async_register(DOMAIN, "set_tunnel_url", _handle_set_tunnel_url)


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
    if entry.runtime_data.audio_adapter is not None:
        await entry.runtime_data.audio_adapter.async_stop()
    if entry.runtime_data.mdns is not None:
        await entry.runtime_data.mdns.async_stop()
    if entry.runtime_data.tls is not None:
        await entry.runtime_data.tls.async_stop()
    await hass.async_add_executor_job(entry.runtime_data.storage.close)
    _LOGGER.info("CasaSmart Hub storage closed")
    return True
