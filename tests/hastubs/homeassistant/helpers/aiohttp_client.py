"""Stub ``homeassistant.helpers.aiohttp_client`` — tests patch the network calls."""


def async_get_clientsession(hass, *args, **kwargs):
    raise RuntimeError("stub: tests must patch the HTTP session")
