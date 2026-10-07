"""CasaSmart hub storage layer.

Public surface:
- HubStorage, KeyValueTable, EnergyEventsTable: the SQLite database (WAL)
  and its table views
- JsonConfigStore: an atomic JSON file for rarely changed config
- Migration, MIGRATIONS, LATEST_VERSION: forward-only schema migrations
- StorageError, MigrationError, ConfigError: the exceptions

Nothing outside this package touches SQLite directly.
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
