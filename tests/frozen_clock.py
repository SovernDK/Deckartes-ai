"""Freezes clock.now for one test, in a fixed time zone."""

import datetime as dt
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from core import clock
from core.config import config

TIMEZONE = "Europe/Warsaw"

def freeze(test: unittest.TestCase, when: str) -> None:
    """Make it local time `when` ("2026-11-11 12:00") in TIMEZONE until the test ends."""
    frozen = dt.datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(TIMEZONE))
    for patcher in (mock.patch.object(clock, "now", return_value=frozen),
                    mock.patch.object(config, "timezone", TIMEZONE)):
        patcher.start()
        test.addCleanup(patcher.stop)
