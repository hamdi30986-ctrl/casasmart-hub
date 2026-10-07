"""``dt_util`` stand-ins used by the athan scheduler."""

import datetime as _datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_time_zone(name):
    return ZoneInfo(name)


async def async_get_time_zone(name):
    """Like HA's: None for an unknown zone, ValueError for a malformed key."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def utcnow():
    return _datetime.datetime.now(_datetime.UTC)
