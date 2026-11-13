"""The current time, conversions to config.timezone and parsing the dates tools get (parse_date), so
"what time is it for the user" has one owner and tests have one thing to freeze."""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from core.config import config


def zone() -> ZoneInfo:
    return ZoneInfo(config.timezone)

_frozen: dt.datetime | None = None

def now() -> dt.datetime:
    return _frozen if _frozen is not None else dt.datetime.now(zone())

@contextmanager
def frozen_at(moment: dt.datetime) -> Iterator[None]:
    global _frozen
    _frozen = moment
    try:
        yield
    finally:
        _frozen = None

def today() -> dt.date:
    return now().date()

def parse_date(text: str) -> dt.date:
    """A date the model gives as YYYY-MM-DD. The model works out "tomorrow" or "friday" itself
    from the current date in the system message; the error names today so it can retry."""
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"Date {text!r} must be YYYY-MM-DD; today is {today():%A %Y-%m-%d}, "
                         f"so work out the date the user means from that")

def to_local(timestamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(timestamp).astimezone(zone())
