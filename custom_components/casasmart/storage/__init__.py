"""Hub storage: the SQLite database, the JSON config file and migrations.

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
