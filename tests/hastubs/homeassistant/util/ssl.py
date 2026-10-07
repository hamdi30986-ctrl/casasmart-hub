"""Stub homeassistant.util.ssl: one shared client context, as in HA."""

import ssl
from functools import cache


@cache
def client_context() -> ssl.SSLContext:
    return ssl.create_default_context()
