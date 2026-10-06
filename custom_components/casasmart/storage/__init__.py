"""CasaSmart hub storage layer (Track B — B1.1).

Public surface:
- HubStorage / KeyValueTable / EnergyEventsTable — SQLite+WAL views
- JsonConfigStore — atomic JSON file for rarely-changed config
- Migration / MIGRATIONS / LATEST_VERSION — forward-only schema migrations
- StorageError / MigrationError / ConfigError — exception hierarchy

Nothing above this package touches SQLite directly. The interface is the
contract; the engine is swappable.
"""

from .config_store import JsonConfigStore
from .exceptions import ConfigError, MigrationError, StorageError
from .migrations import LATEST_VERSION, MIGRATIONS, Migration
from .store import EnergyEventsTable, HubStorage, KeyValueTable

__all__ = [
    "LATEST_VERSION",
    "MIGRATIONS",
    "ConfigError",
    "EnergyEventsTable",
    "HubStorage",
    "JsonConfigStore",
    "KeyValueTable",
    "Migration",
    "MigrationError",
    "StorageError",
]
