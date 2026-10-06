"""Storage-layer exceptions.

Everything the storage package raises derives from StorageError so callers
can catch one base class at the integration boundary.
"""


class StorageError(Exception):
    """Base class for all storage-layer errors."""


class MigrationError(StorageError):
    """A schema migration failed. The database has been restored from backup."""


class ConfigError(StorageError):
    """The JSON config file is unreadable or corrupted."""
