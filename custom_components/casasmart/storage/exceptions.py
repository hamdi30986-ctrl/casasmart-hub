"""CasaSmart runtime component."""


class StorageError(Exception):
    """CasaSmart runtime component."""


class MigrationError(StorageError):
    """CasaSmart runtime component."""


class ConfigError(StorageError):
    """CasaSmart runtime component."""
