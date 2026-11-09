"""Tests for clock, the one owner of the current time and of conversions to config.timezone."""

import datetime as dt
import unittest
from unittest import mock

import clock
from config import config


class ClockTest(unittest.TestCase):
    def in_zone(self, name: str):
        patcher = mock.patch.object(config, "timezone", name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_now_is_in_the_configured_zone(self):
        self.in_zone("Asia/Tokyo")
        self.assertEqual(clock.now().utcoffset(), dt.timedelta(hours=9))

    def test_today_is_the_date_of_now(self):
        moment = dt.datetime(2026, 11, 11, 23, 30)
        with mock.patch.object(clock, "now", return_value=moment):
            self.assertEqual(clock.today(), dt.date(2026, 11, 11))

    def test_to_local_can_move_to_the_next_day(self):
        self.in_zone("Europe/Warsaw")
        self.assertEqual(clock.to_local("2026-11-11T23:30:00Z").isoformat(), "2026-11-12T00:30:00+01:00")

    def test_to_local_reads_an_offset(self):
        self.in_zone("Europe/Warsaw")
        self.assertEqual(clock.to_local("2026-11-11T12:00:00+09:00").isoformat(), "2026-11-11T04:00:00+01:00")

if __name__ == "__main__":
    unittest.main()

class FrozenClockTest(unittest.TestCase):
    MOMENT = dt.datetime(2026, 11, 11, 19, 30, tzinfo=dt.UTC)

    def test_now_and_today_stay_at_the_frozen_moment(self):
        with clock.frozen_at(self.MOMENT):
            self.assertEqual(clock.now(), self.MOMENT)
            self.assertEqual(clock.today(), dt.date(2026, 11, 11))

    def test_the_real_time_returns_after_an_exception(self):
        with self.assertRaises(RuntimeError), clock.frozen_at(self.MOMENT):
            raise RuntimeError
        self.assertLess(abs(clock.now() - dt.datetime.now(dt.UTC)), dt.timedelta(minutes=1))
