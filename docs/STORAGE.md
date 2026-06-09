# Storage Layer (B1.1)

SQLite+WAL behind a dict-like interface, plus an atomic JSON store for
rarely-changed config. **The interface is the contract** — nothing above
this package touches SQLite directly, so the engine is swappable.

## HubStorage — multi-user / frequently-written data

```python
from casasmart.storage import HubStorage

storage = HubStorage(db_path, backup_dir=optional_path)  # default: <db dir>/backups
storage.open()                 # runs migrations (backs up first), enables WAL
users = storage.table("users") # dict-like, lazily created namespace
users["u-123"] = {"name": "Hamdi", "role": "admin"}   # any JSON value
users["u-123"]                 # -> dict (fresh copy; reassign to persist edits)
"u-123" in users, len(users), list(users), users.items()
del users["u-123"]
users.clear()                  # wipe one namespace
users.updated_at("u-123")      # ISO-8601 UTC of last write
storage.schema_version         # current PRAGMA user_version
storage.close()                # checkpoints WAL, closes
```

- Namespaces: `[a-z][a-z0-9_]{0,63}` — anything else raises `ValueError`.
- Keys: non-empty `str` — anything else raises `TypeError`.
- Values: anything `json.dumps` accepts — else `TypeError`, nothing written.
- Thread-safe (RLock). HA's asyncio side calls via executor (B1.2 wraps this).

## Migrations — forward-only, backup-before-run

- Version = `PRAGMA user_version`. Code's migrations live in
  `migrations.MIGRATIONS` (append-only, never edit a shipped step).
- Before any pending migration runs, the DB is snapshotted via the SQLite
  online-backup API (WAL-safe) to `backups/<name>-v<from>-<utc>.db`.
- Each step runs in its own transaction and bumps `user_version` atomically.
- **Failure ⇒ automatic restore** of the pre-run backup + `MigrationError`.
  Never half-applies.
- DB newer than the code ⇒ `MigrationError` (downgrades refused).

## JsonConfigStore — hub identity / network / version pin

```python
from casasmart.storage import JsonConfigStore

cfg = JsonConfigStore(path)        # missing file -> empty; corrupted -> ConfigError
cfg.get("hub_id", default=None)
cfg.set("hub_id", "hub-001")       # persists immediately
cfg.update({...})                  # several keys, one disk write
cfg.delete("key")                  # no-op if absent
cfg.as_dict()                      # snapshot copy
```

Writes are atomic: temp file + fsync + `os.replace`. A crash mid-write can
never leave a half-written config.

## Exceptions

`StorageError` (base) ⟶ `MigrationError`, `ConfigError`.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 30 tests, stdlib only
```
