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
- **A leaked pairing QR is useless off-LAN** — enrollment only completes from a
  LAN source address (`is_lan_request`, `auth_api.py`), checked before anything
  else.

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
- Remote pairing is closed (LAN-gated), so this is not internet-reachable.

## What would close it (future)

Print the hub's identity **fingerprint on the QR / sticker** and have the app
compare the pinned key's fingerprint against it at first contact. That moves the
trust anchor out-of-band and removes the first-contact MITM window entirely. Until
then, the LAN-gated TOFU above is the documented, accepted model.

## Operational guard

`pairing_extra_lan_cidrs` (a dev-only knob for proxied setups, e.g. Docker
Desktop) is **private-CIDR-only** — `is_lan_request` refuses any public entry, so
a misconfiguration cannot widen pairing to the internet. Leave it empty on
production (HAOS/LXC see real peer IPs and never need it).
