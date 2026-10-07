"""Storage-layer exceptions.

Everything the storage package raises derives from StorageError so callers
can catch one base class at the integration boundary.
"""


class StorageError(Exception):
    """Base class for all storage-layer errors."""


class MigrationError(StorageError):
    """A schema migration was refused or failed.

    Refused when the database is newer than this code. When a step fails, the
    database has already been restored from the backup taken before the run.
    """


class ConfigError(StorageError):
    """The JSON config file is unreadable or corrupted."""
