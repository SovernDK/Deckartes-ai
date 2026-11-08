"""The current time and conversions to config.timezone, so "what time is it for the user"
has one owner and tests have one thing to freeze."""

import datetime as dt
from zoneinfo import ZoneInfo

from config import config

def zone() -> ZoneInfo:
    return ZoneInfo(config.timezone)

def now() -> dt.datetime:
    """The current time in config.timezone."""
    return dt.datetime.now(zone())

def today() -> dt.date:
    return now().date()

def to_local(timestamp: str) -> dt.datetime:
    """An ISO 8601 timestamp with a UTC offset or Z, in config.timezone."""
    return dt.datetime.fromisoformat(timestamp).astimezone(zone())
