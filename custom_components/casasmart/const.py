"""CasaSmart runtime component."""

from __future__ import annotations

DOMAIN = "casasmart"

CONFIG_ENTRY_VERSION = 3


DATA_DIR_NAME = "casasmart"
DB_FILENAME = "hub.db"
BACKUP_DIR_NAME = "backups"
HUB_CONFIG_FILENAME = "hub_config.json"




API_VERSION = 1
SUPPORTED_API_VERSIONS = (1,)

MIN_APP_VERSION = "1.0.0"

API_VERSION_HEADER = "X-CasaSmart-API-Version"




TLS_PORT_DEFAULT = 8443




TLS_CERT_CHECK_INTERVAL_HOURS = 24





TUNNEL_WATCHDOG_INTERVAL_MINUTES = 5





MDNS_REFRESH_INTERVAL_MINUTES = 5

HUB_NAME_CONFIG_KEY = "hub_name"
















CONF_CLOUDFLARE_DOMAIN = "cloudflare_domain"
CONF_TUNNEL_ENABLED = "tunnel_enabled"


CONF_PUSH_RELAY_URL = "push_relay_url"





CONF_RELAY_ACTIVATION_CODE = "relay_activation_code"



CONF_RELAY_ACTIVATION_REQUEST_ID = "relay_activation_request_id"





BOOTSTRAP_CODE_HASH_CONFIG_KEY = "bootstrap_code_hash"
RECOVERY_CODE_HASH_CONFIG_KEY = "recovery_code_hash"



PROVISION_SECRET_CONFIG_KEY = "provision_secret"







REMOTE_PAIRING_ENABLED_CONFIG_KEY = "remote_pairing_enabled"







ZIGBEE_BASE_TOPICS_CONFIG_KEY = "zigbee_base_topics"






EVENT_AUTH_CHANGED = "casasmart_auth_changed"





EVENT_REGISTRY_CHANGED = "casasmart_registry_changed"




EVENT_ENERGY_CHANGED = "casasmart_energy_changed"






EVENT_ALARM_CHANGED = "casasmart_alarm_changed"





EVENT_ALARM_TRIGGERED = "casasmart_alarm_triggered"







EVENT_AUDIO_CHANGED = "casasmart_audio_changed"





UPDATE_REPO_CONFIG_KEY = "update_repo"



UPDATE_CHECK_TTL_SECONDS = 6 * 3600












PUSH_RELAY_URL_CONFIG_KEY = CONF_PUSH_RELAY_URL

PUSH_RELAY_PUSH_PATH = "/push"

PUSH_RELAY_REGISTRATION_PATH = "/register-hub"


PUSH_RELAY_TIMEOUT_SECONDS = 10








EVENT_TANK_LOW = "casasmart_tank_low"
EVENT_TANK_OFFLINE = "casasmart_tank_offline"



EVENT_TANK_CHANGED = "casasmart_tank_changed"


PUSH_TYPE_TANK_LOW = "tank_low"
PUSH_TYPE_TANK_OFFLINE = "tank_offline"



PUSH_TYPE_UPDATE_WIDGETS = "update_widgets"



WS_AUTH_TIMEOUT = 30.0


WS_REAUTH_GRACE = 30.0


WS_TOKEN_RECHECK = 60.0







WS_SEND_QUEUE_MAX = 512

WS_CLOSE_AUTH_TIMEOUT = 4000
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_AUTH_EXPIRED = 4002
WS_CLOSE_TOO_SLOW = 4003
