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

# -- LAN TLS (B10) --------------------------------------------------------------
# The CasaSmart API's own HTTPS port (hub_config "tls_port" overrides).
# This — not HA's 8123 — is what the app talks to and what the tunnel routes to.
TLS_PORT_DEFAULT = 8443
# Leaf certs are re-minted inside their renewal margin — checked at startup
# and on this cadence, so rotation needs no human and no fleet-wide expiry
# date ever exists. File names + validity live in tls.py (flat-importable
# for tests, like the other pure modules).
TLS_CERT_CHECK_INTERVAL_HOURS = 24

# -- mDNS discovery (B6) -------------------------------------------------------
# The hub advertises `_casasmart._tcp` (service type owned by discovery.py) so
# the app auto-discovers it on the LAN regardless of DHCP. The advertiser
# re-checks its LAN IP on this cadence and re-publishes if it moved.
MDNS_REFRESH_INTERVAL_MINUTES = 5
# Optional installer-set friendly hub name, surfaced in the mDNS TXT `name`.
HUB_NAME_CONFIG_KEY = "hub_name"

# -- Auth / enrollment (B2) -----------------------------------------------------
# Fired on the HA bus whenever the enrolled-device set changes — a device
# pairs, gets its role/rooms edited, is unpaired, the admin is recovered, or
# the pairing code is regenerated. The `sensor` platform listens to reconcile
# its per-user entities live (add on pair, drop on revoke, repaint on edit).
EVENT_AUTH_CHANGED = "casasmart_auth_changed"

# -- Device registry (B17) -------------------------------------------------------
# Fired on the HA bus after any registry mutation (floors/rooms/devices/
# scenes); the WS server forwards it to connected apps as a
# `registry_changed` frame so they re-fetch through their scoped GET.
EVENT_REGISTRY_CHANGED = "casasmart_registry_changed"

# -- Alarm (B13) ----------------------------------------------------------------
# Fired on the HA bus after any arm-state transition (arm/disarm/pending/
# triggered/tamper). The WS server forwards a content-free `alarm_changed`
# frame to alarm-authorized connections, which re-fetch the gated state GET;
# the alarm adapter also listens to re-sync its entry-delay timer.
EVENT_ALARM_CHANGED = "casasmart_alarm_changed"
# Fired ONLY when an armed zone or a life-safety sensor actually trips the
# alarm (`triggered` / `life_safety`). This is the installer's automation
# hook — siren, lights flash, "whatever Hamdi configures per client" (plan
# B13). Tamper does NOT fire it (offline battery must not sound the house).
# Carries the alarm event dict so an automation can branch on the cause.
EVENT_ALARM_TRIGGERED = "casasmart_alarm_triggered"

# -- Audio (B14) ----------------------------------------------------------------
# Fired on the HA bus whenever the hub's live view of the speakers moves — a
# speaker announces, a retained status/state lands, or one is enrolled/removed.
# The WS server forwards a content-free `audio_changed` frame to audio-
# authorized connections, which re-fetch the gated speaker GET (same pattern as
# `registry_changed` / `alarm_changed`).
EVENT_AUDIO_CHANGED = "casasmart_audio_changed"

# -- Self-update (B5) ----------------------------------------------------------
# The hub checks this GitHub repo's latest release to tell the app an update
# exists. ``owner/name`` form — the installer can repoint it per-fleet via the
# hub_config key below without a code change.
UPDATE_GITHUB_REPO = "casasmart/casasmart-hub"
UPDATE_REPO_CONFIG_KEY = "update_repo"
# Latest-release lookups are cached this long. The status endpoint serves the
# cache and refreshes lazily past this age — GitHub is polled at most once per
# window, never once per request (and never on the hot path of a tile refresh).
UPDATE_CHECK_TTL_SECONDS = 6 * 3600

# -- Push relay (B8) ------------------------------------------------------------
# The hub signs each push batch with its permanent Ed25519 push-identity key
# (push_crypto.py) and POSTs it to the relay; the relay authenticates the
# signature against the hub's out-of-band-registered public key and fans the
# batch out to FCM. The relay base URL is installer-overridable per fleet via
# the hub_config key below — repointing a fleet needs no code change. The
# default targets the production Deno Deploy relay (the ``casasmart-relay``
# project on console.deno.com).
PUSH_RELAY_URL_DEFAULT = "https://casasmart-relay.deno.dev"
PUSH_RELAY_URL_CONFIG_KEY = "push_relay_url"
# Path appended to the relay base URL for the push endpoint.
PUSH_RELAY_PUSH_PATH = "/push"
# Outbound POST timeout (seconds). A wedged relay must never block the event
# loop's alarm/lock reaction longer than this — the send is fire-and-forget.
PUSH_RELAY_TIMEOUT_SECONDS = 10

# -- Water tank (B8 Piece 4b) --------------------------------------------------
# The hub owns the voltage→percent calibration AND the user-tunable low-water
# threshold (the app is pure UI now — it sends inputs, reads computed values
# back). These two bus events are the installer-automation hooks, the same role
# EVENT_ALARM_TRIGGERED plays for the alarm: the daily 18:00-AST low-level sweep
# fires EVENT_TANK_LOW, the 20-minute-silence watchdog fires EVENT_TANK_OFFLINE,
# each carrying the device id + the relevant levels so an automation can branch.
EVENT_TANK_LOW = "casasmart_tank_low"
EVENT_TANK_OFFLINE = "casasmart_tank_offline"
# Carried in the plaintext push payload's ``data.type`` so the app localizes and
# routes the notification (mirrors PUSH_TYPE_SECURITY / PUSH_TYPE_LOCK).
PUSH_TYPE_TANK_LOW = "tank_low"
PUSH_TYPE_TANK_OFFLINE = "tank_offline"

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
