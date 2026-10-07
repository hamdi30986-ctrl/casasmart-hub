"""Constants shared across the CasaSmart integration.

Domain, storage paths, API versions, config and hub_config keys, event names,
factory-reset tables, and push relay and WebSocket settings. Constants used by
a single module live in that module instead.
"""

from __future__ import annotations

DOMAIN = "casasmart"

# Notifications that show an owner (bootstrap) code; dismissed once the hub is
# claimed, since the code is spent.
NOTIFY_BOOTSTRAP_CODE = f"{DOMAIN}_bootstrap_pairing"
NOTIFY_REGENERATED_CODE = f"{DOMAIN}_regenerated_pairing"

# Config entry schema version; async_migrate_entry upgrades older entries.
CONFIG_ENTRY_VERSION = 3

# -- Storage (all under /config/casasmart) ------------------------------------

DATA_DIR_NAME = "casasmart"
DB_FILENAME = "hub.db"
BACKUP_DIR_NAME = "backups"
HUB_CONFIG_FILENAME = "hub_config.json"

# -- App API versioning (served by the handshake) -----------------------------

API_VERSION = 1
SUPPORTED_API_VERSIONS = (1,)
MIN_APP_VERSION = "1.0.0"
# Optional request header naming the API version the app speaks; the handshake
# answers whether this hub supports it.
API_VERSION_HEADER = "X-CasaSmart-API-Version"

# -- Listeners and periodic checks --------------------------------------------

TLS_PORT_DEFAULT = 8443
TLS_CERT_CHECK_INTERVAL_HOURS = 24
TUNNEL_WATCHDOG_INTERVAL_MINUTES = 5
MDNS_REFRESH_INTERVAL_MINUTES = 5

# -- Config entry: options and data -------------------------------------------

CONF_CLOUDFLARE_DOMAIN = "cloudflare_domain"
CONF_TUNNEL_ENABLED = "tunnel_enabled"
CONF_PUSH_RELAY_URL = "push_relay_url"
# Entry data, kept only until the relay accepts the registration.
CONF_RELAY_ACTIVATION_CODE = "relay_activation_code"
CONF_RELAY_ACTIVATION_REQUEST_ID = "relay_activation_request_id"

# -- hub_config.json keys -----------------------------------------------------

HUB_NAME_CONFIG_KEY = "hub_name"
# Hashes of the permanent owner codes (the printed pairing sticker and the
# recovery card), reinstalled at every boot; a factory reset deletes both.
BOOTSTRAP_CODE_HASH_CONFIG_KEY = "bootstrap_code_hash"
RECOVERY_CODE_HASH_CONFIG_KEY = "recovery_code_hash"
# The shared key speakers present to fetch their broker settings.
PROVISION_SECRET_CONFIG_KEY = "provision_secret"
# Only the JSON value true lets GET /audio/provision serve LAN clients without
# the provisioning key. Off by default: the response carries the broker password.
KEYLESS_SPEAKER_PROVISIONING_CONFIG_KEY = "keyless_speaker_provisioning"
REMOTE_PAIRING_ENABLED_CONFIG_KEY = "remote_pairing_enabled"
ZIGBEE_BASE_TOPICS_CONFIG_KEY = "zigbee_base_topics"
UPDATE_REPO_CONFIG_KEY = "update_repo"
# Where config entries before version 3 kept the relay URL; the migration moves
# it into the entry options and deletes it here.
PUSH_RELAY_URL_CONFIG_KEY = CONF_PUSH_RELAY_URL

# -- Home Assistant bus events ------------------------------------------------

EVENT_AUTH_CHANGED = "casasmart_auth_changed"
EVENT_REGISTRY_CHANGED = "casasmart_registry_changed"
EVENT_ENERGY_CHANGED = "casasmart_energy_changed"
EVENT_ALARM_CHANGED = "casasmart_alarm_changed"
EVENT_ALARM_TRIGGERED = "casasmart_alarm_triggered"
EVENT_AUDIO_CHANGED = "casasmart_audio_changed"
EVENT_TANK_CHANGED = "casasmart_tank_changed"
EVENT_SUGGESTIONS_CHANGED = "casasmart_suggestions_changed"
EVENT_TANK_LOW = "casasmart_tank_low"
EVENT_TANK_OFFLINE = "casasmart_tank_offline"

# -- Factory reset ------------------------------------------------------------

# Storage tables casasmart.factory_reset clears: the app layer plus the
# registry organization layer (floors, rooms, tags, assignments, grouping and
# the room-move receipts), which re-seeds from Home Assistant on reload.
FACTORY_RESET_TABLES = (
    "auth_devices",
    "pairing_codes",
    "recovery_codes",
    "registry_favorites",
    "registry_scenes",
    "user_settings",
    "now_recents",
    "now_room_policies",
    "now_config",
    "now_restore_sets",
    "now_idempotency",
    "suggestions_v1",
    "push_tokens",
    "hq_notifications",
    "alarm_history",
    "alarm_state",
    "audio_config",
    "audio_speakers",
    "energy_configs",
    "energy_state",
    "energy_flags",
    "registry_floors",
    "registry_rooms",
    "registry_room_tags",
    "registry_room_moves",
    "registry_devices",
    "registry_user_devices",
)
# Tables a factory reset keeps, because they describe the house rather than
# its owner: the alarm's sensor zones and settings, and the tanks.
FACTORY_RESET_KEPT_TABLES = ("alarm_settings", "alarm_zones", "tank_devices")

# -- Built-in updater ---------------------------------------------------------

# Ed25519 public key that signs every published casasmart.zip. The release
# script signs with the matching private key, which lives only on the release
# host; the built-in updater refuses any artifact this key did not sign.
UPDATE_SIGNING_PUBLIC_KEY_B64 = "uqXZj9IlXOTMEdaJ4gest3HawiuRsc62GNxORB5HIAY="
# How long a GitHub release check is reused before asking again.
UPDATE_CHECK_TTL_SECONDS = 6 * 3600

# -- Push relay ---------------------------------------------------------------

PUSH_RELAY_PUSH_PATH = "/push"
PUSH_RELAY_REGISTRATION_PATH = "/register-hub"
PUSH_RELAY_TIMEOUT_SECONDS = 10
PUSH_TYPE_TANK_LOW = "tank_low"
PUSH_TYPE_TANK_OFFLINE = "tank_offline"
# Silent push that tells the apps to refresh their home-screen widgets.
PUSH_TYPE_UPDATE_WIDGETS = "update_widgets"

# -- WebSocket (seconds, frames and close codes) ------------------------------

# Time a new socket has to send its auth frame.
WS_AUTH_TIMEOUT = 30.0
# Time a socket whose token went stale has to re-authenticate.
WS_REAUTH_GRACE = 30.0
# How often an open socket's token is checked again (expiry, unpair, edits).
WS_TOKEN_RECHECK = 60.0
# Outbound frames buffered per socket. State updates coalesce or are dropped
# past it; a socket whose buffer fills with protocol frames is closed with
# WS_CLOSE_TOO_SLOW.
WS_SEND_QUEUE_MAX = 512
WS_CLOSE_AUTH_TIMEOUT = 4000
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_AUTH_EXPIRED = 4002
WS_CLOSE_TOO_SLOW = 4003
