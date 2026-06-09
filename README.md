# casasmart-hub

CasaSmart's Home Assistant custom integration — the hub-side core from the
CSv1 launch plan (Track B). Lives at `custom_components/casasmart/` on every
client hub; this repo is the source of truth and the future HACS channel.

## Layout

```
custom_components/casasmart/   # the integration (what HACS installs)
  storage/                     # B1.1 — SQLite+WAL + JSON config (docs/STORAGE.md)
tests/                         # stdlib unittest, no dependencies
docs/                          # module docs
```

## Dev environment

- Dev HA: `http://192.168.8.235:8124` (container `homeassistant-dev`,
  config at `/storage/docker/config/homeassistant-dev`) — B1.0
- Live HA on `:8123` is never touched by dev work.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

## Build order (B1 spine)

B1.0 dev env ✅ → B1.1 storage ✅ → B1.2 scaffold → B1.3 handshake/REST →
B1.4 entity bridge → B1.5 WebSocket → B1.6 auth engine.
Full plan: `/Users/macmini/CSv1/csv1-launch-plan.md`
