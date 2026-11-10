"""The current time and conversions to config.timezone, so "what time is it for the user"
has one owner and tests have one thing to freeze."""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from core.config import config


def zone() -> ZoneInfo:
    return ZoneInfo(config.timezone)

# Set by frozen_at
_frozen: dt.datetime | None = None

def now() -> dt.datetime:
    """The current time in config.timezone, or the frozen moment inside frozen_at."""
    return _frozen if _frozen is not None else dt.datetime.now(zone())

@contextmanager
def frozen_at(moment: dt.datetime) -> Iterator[None]:
    """Make it always `moment` inside the block, e.g. so a validation run gets the same dates each time."""
    global _frozen
    _frozen = moment
    try:
        yield
    finally:
        _frozen = None

def today() -> dt.date:
    return now().date()

def to_local(timestamp: str) -> dt.datetime:
    """An ISO 8601 timestamp with a UTC offset or Z, in config.timezone."""
    return dt.datetime.fromisoformat(timestamp).astimezone(zone())
