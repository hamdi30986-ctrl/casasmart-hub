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

# -- WebSocket server (B1.5) ---------------------------------------------------
# First frame after connect must be the auth frame within this window.
WS_AUTH_TIMEOUT = 30.0
# Token invalidated mid-connection -> `auth_required`, this long to re-auth
# (plan, audit round 5: "30s grace to supply a fresh token, else clean close").
WS_REAUTH_GRACE = 30.0
# How often a live connection's token is re-checked against the auth backend
# (catches revocation/expiry without waiting for the next request).
WS_TOKEN_RECHECK = 60.0
# Outbound push queue per connection; a consumer this far behind is dead or
# too slow to be useful — close instead of buffering unbounded.
WS_SEND_QUEUE_MAX = 256
# WS close codes (4000-4999 = application-defined).
WS_CLOSE_AUTH_TIMEOUT = 4000
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_AUTH_EXPIRED = 4002
WS_CLOSE_TOO_SLOW = 4003
