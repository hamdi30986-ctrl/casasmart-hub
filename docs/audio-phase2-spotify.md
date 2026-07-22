# Audio Phase 2 — Spotify + synchronized multi-room

**Status:** planned, deferred (execute the week of 2026-07-07).
**Author:** design review, 2026-07-04.
**Prereq shipped:** real AirPlay transport control (play/pause/next/prev) — see the
end of this doc; that landed 2026-07-04 and is independent of everything below.

The goal is Sonos-class behaviour: the **hub** holds the Spotify account, the **hub**
decodes the audio, and the **hub** streams it to the speakers **in sync**. The phone
only ever sends control. Nothing plays from the phone; picking a non-hub Spotify
Connect target does nothing in our app.

This plan is written to be **appliance-grade**: the installer bakes every moving part
into the golden image and an HA add-on, tested once by us and cloned. Neither the operator nor
a client ever touches a Snapcast/librespot config file. That is the whole point — we are
not "configuring Snapcast," we are shipping a product that happens to use it.

---

## 1. Why Snapcast, and why it will not repeat the 2025 pain

The three things that made this miserable a year ago, and why each is designed out here:

| Old pain | Why it happened | How it's gone now |
|---|---|---|
| **Typing the server IP into each client** | manual `--host` config | `snapclient` with **no host** auto-discovers `_snapcast._tcp` over **mDNS**. Avahi is already running on the speaker (shairport uses it). The client config contains no address. |
| **FIFO / named-pipe glue bugs** | hand-authored `/tmp/snapfifo`, format mismatch | The pipe lives **only on the hub**, never on the speaker. Snapserver 0.31 has **built-in `librespot` / `airplay` stream types** that manage the pipe internally. We own both ends, so the format is **hard-locked to `48000:16:2`** — mismatch was the #1 cause of the garbage/underruns. |
| **Fragile, per-unit setup** | glue assembled live | Config is baked into the **golden image** (`image_prep.sh`) + an **HA add-on**. Provisioned once, cloned to all units. Self-heals via systemd restart + mDNS re-discovery. |

Confirmed facts (2026-07-04, speaker `965cb9`, Debian 13 trixie / aarch64):
- `snapclient` is in **apt at 0.31.0** — no source builds.
- **Avahi active**, `avahi-browse` present → mDNS discovery works.
- Speaker software installs via **plain shell** (`deploy-remote.sh` + `image_prep.sh`) —
  trivially extended with `apt install snapclient` + a systemd drop-in.

Residual risks we accept going in:
- Only **one Pi3** on hand — we can prove server↔client discovery, format-lock and
  audible single-client sync now; **true multi-speaker sync robustness needs 2+ units**
  (Pi4 fleet, ~2 weeks out).
- The add-on's Supervisor auto-install needs a **real HA-OS hub** to validate (dev hub is
  plain Docker, no Supervisor) — validate on the `pve→haos` test box.

---

## 2. Architecture

```
  App (phone)                    HUB (source of truth)                 Speakers (Pi)
  ───────────                    ─────────────────────                 ─────────────
  Connect Spotify ── OAuth ─────▶ token store (refresh token, hub-only)
                                  librespot  ◀═══ Spotify streams AUDIO here
  transport/browse ─ REST ──────▶   │ PCM (48000:16:2)
  now-playing      ◀── Web API ──   ▼
                                  snapserver ════ mDNS ════▶ snapclient ─▶ ALSA (dmix) × N
                                                             (sample-accurate, ~1 ms LAN)
```

Three concerns, all on the hub:
- **Account:** OAuth refresh token stored hub-side, never on the phone.
- **Decode:** `librespot` is the Spotify Connect device "CasaSmart"; Spotify streams to it.
- **Sync:** `snapserver` timestamps every chunk; `snapclient`s render the same sample at
  the same instant via Snapcast's own clock protocol (independent of NTP).

---

## 3. The two packaged artifacts

### 3a. Speaker side — bake `snapclient` into the image

Add to **both** install paths (they are the same few lines):

- `deploy-remote.sh` (the desktop `casasmart_speaker.app` per-Pi installer) and
- `image_prep.sh` (golden-image builder), step 6 where services are enabled.

```
apt-get install -y snapclient
# systemd drop-in: no --host (mDNS), fixed output to the HiFiBerry/dmix device,
#   --sampleformat 48000:16:2, soundcard = the same ALSA device shairport uses.
systemctl enable --now snapclient
```

The client has **no server address and no pipe**. It receives and plays. Done.

> The only payload change is adding `snapclient`; `cs_payload.tar.gz` is otherwise
> current (as of 2026-07-04 the payload `app.py` matches the live agent, and it already
> ships shairport with `enable_remote="yes"` — so transport needs no payload change).

### 3b. Hub side — a "CasaSmart Audio" HA add-on

Snapserver + librespot cannot run inside the Python integration (HA core has no apt /
system audio). The HA-native vehicle is an **add-on** (Docker), installed from the
CasaSmart add-on repository. The `casasmart` integration **auto-installs and starts it via
the Supervisor API** — exactly how ESPHome / Matter / Z-Wave JS ship their add-ons. To the
client it's one click: install the integration, the audio backend provisions itself.

Add-on contents (all pinned, all baked):
- `snapserver` with a `librespot` stream source (format `48000:16:2`), advertised over
  Avahi as `_snapcast._tcp`.
- `librespot` authed from the hub's stored Spotify token.
- Snapserver JSON-RPC control on `:1705` (the integration talks to this for group/stream
  state) — never exposed to the phone.

Dev note: on the plain-Docker dev hub (no Supervisor) run the add-on image as a **sibling
container** by hand for testing; the Supervisor path is validated on `pve→haos`.

---

## 4. Spotify: auth, control, browse

### Auth (OAuth, hub-exclusive)
- App Settings → **Connect Spotify** → in-app webview → **Authorization Code + PKCE**.
- Redirect URI is a **hub URL** (through the Cloudflare tunnel). The hub exchanges the
  code for a refresh token and stores it (hub-only, survives phone changes).
- Scopes: `user-read-playback-state user-modify-playback-state streaming
  playlist-read-private user-library-read user-read-currently-playing`.

### Control + browse (hub proxies the Spotify Web API)
The app never calls Spotify directly. New hub endpoints under `/audio/spotify/*`, all
`audio.control` (playback) or `audio.read` (browse), that proxy to the Web API with the
stored token and target the hub's librespot device:
- `POST /audio/spotify/transport` — play / pause / next / previous / seek.
- `POST /audio/spotify/play` — start a playlist / album / track / search result URI.
- `GET  /audio/spotify/now-playing` — track, art, position, is_playing.
- `GET  /audio/spotify/playlists`, `GET /audio/spotify/search?q=…`.
Because the only Connect target we expose is the hub, playback can only land on the
speakers.

### Premium + ToS reality (product decision, not code)
- `librespot` is **unofficial**; commercial use is against Spotify's ToS. Fine for
  the operator's own villa + demos; **before selling broadly**, move to Spotify's **certified
  partner** program ("Spotify for Speakers"). The architecture is identical — swap the
  librespot box for the certified SDK; snapcast, OAuth-on-hub, Web-API control and the
  whole app UI are unchanged, so librespot-now is **not** throwaway work.
- Requires the customer to have **Spotify Premium** (free tier can't Connect-stream).

---

## 5. The priority / ducking contract (all sources coexist)

The Pi will have up to three audio producers contending for ALSA `dmix`:
`shairport-sync` (AirPlay), `snapclient` (Spotify/music), and the agent's clip player
(athan / PA via `play_at`). Rules the agent enforces:

1. **Athan / PA is top priority** — on a priority clip, the agent pauses/ducks snapclient
   and (via DACP) pauses AirPlay, plays the clip, then restores.
2. **Music base layer** — AirPlay and Spotify are mutually exclusive in practice (starting
   one stops the other); the agent tracks the active source.
3. **North star** — eventually route AirPlay + clips through snapserver too, so there is a
   **single sync engine**. Not day one (it changes "each speaker is its own AirPlay
   endpoint"), but the target.

---

## 6. Execution order (next week)

1. **Spike (½ day):** `apt install snapclient` on the Pi3; snapserver container on the
   hub with a format-locked test stream; snapclient with **no host** → discovers + plays.
   The operator listens: no IP, no pipe, it just works. Gate: if ugly, fall back to
   **single-speaker Spotify first** (no sync engine) and add sync later.
2. **Speaker bake:** snapclient into `deploy-remote.sh` + `image_prep.sh`; refresh
   `cs_payload.tar.gz`.
3. **Hub add-on:** snapserver + librespot image; integration auto-install via Supervisor;
   validate on `pve→haos`.
4. **Spotify auth:** webview + PKCE + hub token store + refresh.
5. **Spotify control/browse:** the `/audio/spotify/*` endpoints + Web API proxy.
6. **App UI:** Settings → Connect Spotify; now-playing + playlists + search; transport
   already exists from Phase-2-pre.
7. **Multi-speaker validation** on the Pi4 fleet; ducking contract end-to-end.
8. **(Later)** certified-partner migration before broad sale.

---

## Appendix — shipped 2026-07-04: real AirPlay transport control

Independent of Spotify, and the foundation for the transport UI:
- shairport-sync on every speaker already runs with `enable_remote = "yes"` +
  `publish_parsed = "yes"`, topic `speakers/<mac6>/airplay` (baked by the installer's
  `provision.py`). So DACP remote works today with **no speaker or installer change**.
- Hub: `POST /audio/speakers/{mac6}/airplay {action}` publishes the raw DACP verb
  (`playpause` / `nextitem` / `previtem`) to `speakers/<mac6>/airplay/remote`; shairport
  relays it to the AirPlay **source** — the iPhone genuinely pauses/skips.
- App: prev / play-pause / next in the speaker tile; the play/pause icon reflects
  `HubSpeakerLive.airplayActive`.
