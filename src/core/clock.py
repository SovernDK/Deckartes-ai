"""The current time, conversions to config.timezone and the days users name (parse_day), so
"what time is it for the user" has one owner and tests have one thing to freeze."""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from core.config import config

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

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

def parse_day(text: str) -> dt.date:
    """The local day the user named: "today", "tomorrow", "yesterday", a weekday ("friday" is the
    next one, today included; "next friday" is after today) or YYYY-MM-DD. ValueError otherwise."""
    key = text.strip().lower()
    now = today()
    relative = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if key in relative:
        return now + dt.timedelta(days=relative[key])
    after_today = key.startswith("next ")
    weekday = key.removeprefix("next ").strip()
    if weekday in WEEKDAYS:
        days = (WEEKDAYS.index(weekday) - now.weekday()) % 7
        if days == 0 and after_today:
            days = 7
        return now + dt.timedelta(days=days)
    try:
        return dt.date.fromisoformat(key)
    except ValueError:
        raise ValueError(f"Date {text!r} must be today, tomorrow, yesterday, a weekday or YYYY-MM-DD")

def to_local(timestamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(timestamp).astimezone(zone())
