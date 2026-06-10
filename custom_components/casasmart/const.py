"""Constants for the CasaSmart Hub integration."""

from __future__ import annotations

DOMAIN = "casasmart"

# Everything the hub persists lives under <ha-config>/casasmart/.
DATA_DIR_NAME = "casasmart"
DB_FILENAME = "hub.db"
BACKUP_DIR_NAME = "backups"
HUB_CONFIG_FILENAME = "hub_config.json"

# -- API version contract (plan: "API Version Handshake") ---------------------
# Bumped only for breaking changes (auth flow, wire format, removed endpoint).
# Additive changes (new endpoint, new response field) never bump it.
API_VERSION = 1
SUPPORTED_API_VERSIONS = (1,)
# Oldest app version this hub will talk to.
MIN_APP_VERSION = "1.0.0"
# Header the app sends to declare which API version it speaks.
API_VERSION_HEADER = "X-CasaSmart-API-Version"
