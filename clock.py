"""The current time, so everything that depends on it has one place tests can freeze."""

import datetime as dt
from zoneinfo import ZoneInfo

from config import config

def now() -> dt.datetime:
    """The current time in config.timezone."""
    return dt.datetime.now(ZoneInfo(config.timezone))
