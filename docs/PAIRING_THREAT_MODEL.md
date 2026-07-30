# Pairing threat model (TOFU)

Companion to [FIRST_PAIR.md](FIRST_PAIR.md). Records the trust assumptions of
first-pairing so the tradeoff below is a **conscious sign-off**, not a silent one.

## The trust anchor: identity-key pinning

The hub has a permanent **identity key** (P-256, generated once, `0600`, never
auto-rekeyed — `tls.py`). Its public key (SPKI) and a SHA-256 fingerprint are
served at `GET /handshake` (`api.py`). The app **pins** that key on first pairing
and validates every later TLS leaf by checking the leaf's signature against the
pinned identity key. Leaf certs are short-lived and identity-signed, so:

- **Cert substitution is useless** — a swapped leaf not signed by the pinned
  identity fails validation; leaf rotation is invisible to paired phones because
  the pin never moves.
- **A leaked bootstrap (owner-claim) code is useless off-LAN — always.**
  Code-class policy (pairing redesign Phase 1): every pairing code carries a
  class, `bootstrap` or `member` (`pairing.py`). The bootstrap owner claim is
  LAN-only unconditionally — enforced inside `redeem()` itself
  (`LanOnlyCodeError`), not just at the HTTP gate, so no call site can forget
  it. First claim = physical possession.
- **A leaked member code is useless off-LAN while `remote_pairing_enabled`
  is off (the default).** The enroll gate (`is_lan_request`, `auth_api.py`)
  refuses non-LAN sources exactly as before unless the hub_config flag
  `remote_pairing_enabled` is strictly `true`. With the flag ON, an
  admin-minted **member** code is deliberately redeemable from any source
  (LAN or tunnel) — that is the feature (family pairs from anywhere), and
  the accepted exposure is bounded by what a code already is: single-use,
  SHA-256-hashed at rest, per-source escalating redeem throttle, and (Phase
  5) an admin push notification on every enroll.

## The accepted residual risk: TOFU at first contact

The pin is delivered as a **plain JSON body over the TLS transport at first
contact** — it is *not* wrapped in a signature from a pre-shared secret, and the
QR/sticker does **not** currently commit to the fingerprint out-of-band.

**Threat:** an active man-in-the-middle present on the **same LAN at the very
first pairing** could answer `/handshake` with *its own* identity and get the app
to pin the attacker. All later traffic would then validate against the
attacker's key.

**Scope / why it's accepted:**
- It requires an on-path attacker on the local network *during the one-time
  pairing window* — the standard, widely-accepted TOFU posture for consumer smart
  home (matches SSH-on-first-connect, most LAN-paired hubs).
- Everything after first pairing is cryptographically pinned; re-pairing an
  already-enrolled key grants nothing on its own (login still needs the device
  private key).
- The bootstrap owner claim — the enrollment that *establishes* the household
  — is LAN-gated unconditionally (code-class policy above), so first contact
  is never internet-reachable. Member enrolls become internet-reachable only
  when `remote_pairing_enabled` is switched on, and the anchor for those is
  the QR fingerprint below, not bare TOFU.

## Closing the TOFU window: the QR fingerprint anchor

The mitigation this document previously listed as future work is now the
committed design (pairing redesign, payload v2 — Phases 3/4): the mint
response carries the hub's **`identity_fingerprint`** (and `tunnel_url`)
inside the QR / deep-link payload, and the app compares the handshake
identity against that out-of-band fingerprint **before pinning** — local or
remote. The trust anchor moves from "whoever answered `/handshake` first"
to "the admin-minted payload itself":

- **LAN first contact:** the same-LAN MITM window closes — an attacker
  answering `/handshake` cannot match the fingerprint committed in the QR.
- **Remote member enroll (flag on):** the phone dials the payload's
  `tunnel_url` over public-CA TLS and still verifies the handshake identity
  against the payload fingerprint, so a remote enroll never trusts an
  unanchored first contact at all.

Until the payload-v2 phases land in the app, the LAN-gated TOFU above (with
`remote_pairing_enabled` off) remains the operative, accepted model.

## Cloud posture at install (Phase 2)

A fresh install no longer auto-disables the Cloudflare tunnel: the config
flow (and the `set_tunnel_url` installer service) seeds
`tunnel_enabled: true`, so the tunnel is up from day one and the handshake
advertises `tunnel.url` immediately — phones capture the remote path at
pairing instead of hitting the "paired while cloud was off, never learned
the URL" dead zone. This does not widen the pairing surface by itself: the
enroll gate's policy (LAN-only unless `remote_pairing_enabled`) is
independent of whether the tunnel is running, and the current app pairs
against the hub's **LAN address directly** (mDNS → pinned TLS), never via
the Cloudflare domain — the historical local-phones-blocked-via-CF failure
that motivated auto-disable belonged to the legacy app's routing. The
options-flow toggle remains as a manual emergency off-switch.

## Operational guard

`pairing_extra_lan_cidrs` (a dev-only knob for proxied setups, e.g. Docker
Desktop) is **private-CIDR-only** — `is_lan_request` refuses any public entry, so
a misconfiguration cannot widen pairing to the internet. Leave it empty on
production (HAOS/LXC see real peer IPs and never need it).
