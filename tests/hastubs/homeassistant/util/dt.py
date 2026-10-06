"""``dt_util`` stand-ins used by the athan scheduler."""

import datetime as _datetime
from zoneinfo import ZoneInfo


def get_time_zone(name):
    return ZoneInfo(name)


def utcnow():
    return _datetime.datetime.now(_datetime.timezone.utc)
