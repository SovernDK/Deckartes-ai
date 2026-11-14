"""The text the calendar tools give the model, with EventKit faked as docs/calendar-tests.md describes."""

import datetime as dt
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from Foundation import NSDate

from frozen_clock import TIMEZONE, freeze
from tools import calendar_api as calendar_tools
from tools import tools


def local(when: str) -> dt.datetime:
    return dt.datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(TIMEZONE))

class FakeCalendar:
    def __init__(self, name: str):
        self.name = name

    def title(self) -> str:
        return self.name

class FakeEvent:
    """The parts of an EKEvent the backend reads."""

    def __init__(self, title: str, start: str, end: str, *, all_day: bool = False, location: str | None = None,
                 notes: str | None = None):
        self.start, self.end = local(start), local(end)
        self.values = (title, location, notes)
        self.all_day = all_day

    def title(self):
        return self.values[0]

    def location(self):
        return self.values[1]

    def notes(self):
        return self.values[2]

    def startDate(self):
        return NSDate.dateWithTimeIntervalSince1970_(self.start.timestamp())

    def endDate(self):
        return NSDate.dateWithTimeIntervalSince1970_(self.end.timestamp())

    def isAllDay(self) -> bool:
        return self.all_day

    def calendar(self) -> FakeCalendar:
        return FakeCalendar("Home")

class CalendarToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        freeze(self, "2026-11-11 12:00")  # a Wednesday
        self.events: list[FakeEvent] = []
        self.questions: list[str] = []
        self.answer = True
        self.save = mock.Mock()
        for target, name, value in ((calendar_tools, "_events_between", self.between),
                                    (calendar_tools, "_target_calendar", lambda: FakeCalendar("Home")),
                                    (calendar_tools, "save_event", self.save)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def between(self, start: dt.datetime, end: dt.datetime) -> list[FakeEvent]:
        return sorted((e for e in self.events if e.start < end and e.end > start), key=lambda e: e.start)

    async def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.answer

    async def run_tool(self, name: str, arguments: dict) -> str:
        """Run a tool as a turn would, asking the user through self.confirm."""
        return await tools.run_tool(name, arguments, self.confirm)

class ReadTest(CalendarToolTest):
    async def test_lists_only_the_asked_days_events_with_location_on_one_line(self):
        self.events = [FakeEvent("Dentist", "2026-11-12 14:30", "2026-11-12 15:00", location="Clinic\nMain St"),
                       FakeEvent("Elsewhere", "2026-11-13 09:00", "2026-11-13 10:00")]
        self.assertEqual(await tools.get_calendar_events("2026-11-12"),
                         "Calendar events for Thursday 2026-11-12 (local time):\n"
                         "- Dentist: 14:30-15:00 (calendar Home; at Clinic, Main St)")

    async def test_day_without_events_says_so(self):
        self.assertEqual(await tools.get_calendar_events("2026-11-12"),
                         "No events in the calendar for Thursday 2026-11-12.")

    async def test_all_day_event_has_no_times(self):
        self.events = [FakeEvent("Holiday", "2026-11-12 00:00", "2026-11-12 23:59:59", all_day=True)]
        self.assertEqual(await tools.get_calendar_events("2026-11-12"),
                         "Calendar events for Thursday 2026-11-12 (local time):\n"
                         "- Holiday: all day (calendar Home)")

    async def test_a_day_name_is_refused_with_today_so_the_model_can_retry(self):
        self.assertEqual(await self.run_tool("get_calendar_events", {"date": "tomorrow"}),
                         "Tool get_calendar_events failed: Date 'tomorrow' must be YYYY-MM-DD; today is "
                         "Wednesday 2026-11-11, so work out the date the user means from that")

    async def test_rest_of_the_week_grouped_by_day(self):
        self.events = [FakeEvent("Trip", "2026-11-09 08:00", "2026-11-12 18:00"),
                       FakeEvent("Gym", "2026-11-14 10:00", "2026-11-14 11:00"),
                       FakeEvent("Next week", "2026-11-16 10:00", "2026-11-16 11:00")]
        self.assertEqual(await tools.get_calendar_period("this week"),
                         "Calendar events for the rest of this week, Wednesday 2026-11-11 to Sunday 2026-11-15 "
                         "(local time; days not listed have no events):\n"
                         "Wednesday 2026-11-11:\n"
                         "- Trip: Monday 2026-11-09 08:00 to Thursday 2026-11-12 18:00 (calendar Home)\n"
                         "Thursday 2026-11-12:\n"
                         "- Trip: Monday 2026-11-09 08:00 to Thursday 2026-11-12 18:00 (calendar Home)\n"
                         "Saturday 2026-11-14:\n"
                         "- Gym: 10:00-11:00 (calendar Home)")

    async def test_one_day_inside_a_longer_event_gives_its_dates(self):
        self.events = [FakeEvent("Trip", "2026-11-09 08:00", "2026-11-12 18:00")]
        self.assertEqual(await tools.get_calendar_events("2026-11-11"),
                         "Calendar events for Wednesday 2026-11-11 (local time):\n"
                         "- Trip: Monday 2026-11-09 08:00 to Thursday 2026-11-12 18:00 (calendar Home)")

    async def test_all_day_event_over_several_days_is_listed_on_each(self):
        self.events = [FakeEvent("Holiday", "2026-11-12 00:00", "2026-11-13 23:59:59", all_day=True)]
        line = "- Holiday: all day, Thursday 2026-11-12 to Friday 2026-11-13 (calendar Home)"
        self.assertEqual(await tools.get_calendar_period("this week"),
                         "Calendar events for the rest of this week, Wednesday 2026-11-11 to Sunday 2026-11-15 "
                         "(local time; days not listed have no events):\n"
                         f"Thursday 2026-11-12:\n{line}\nFriday 2026-11-13:\n{line}")

    async def test_event_over_the_end_of_the_period_stops_at_its_last_day(self):
        self.events = [FakeEvent("Trip", "2026-11-15 08:00", "2026-11-17 18:00")]
        self.assertEqual((await tools.get_calendar_period("this week")).split("\n")[1:],
                         ["Sunday 2026-11-15:",
                          "- Trip: Sunday 2026-11-15 08:00 to Tuesday 2026-11-17 18:00 (calendar Home)"])

    async def test_event_ending_at_midnight_is_not_on_the_next_day(self):
        self.events = [FakeEvent("Party", "2026-11-13 22:00", "2026-11-14 00:00")]
        self.assertEqual((await tools.get_calendar_period("this week")).split("\n")[1:],
                         ["Friday 2026-11-13:", "- Party: 22:00 to Saturday 2026-11-14 00:00 (calendar Home)"])

    async def test_periods_name_their_days(self):
        expected = {"next week": "next week, Monday 2026-11-16 to Sunday 2026-11-22",
                    "this month": "the rest of this month, Wednesday 2026-11-11 to Monday 2026-11-30",
                    "next month": "next month, Tuesday 2026-12-01 to Thursday 2026-12-31",
                    "january": "January 2027, Friday 2027-01-01 to Sunday 2027-01-31",
                    "2026-12": "December 2026, Tuesday 2026-12-01 to Thursday 2026-12-31"}
        for period, span in expected.items():
            with self.subTest(period):
                self.assertEqual(await tools.get_calendar_period(period), f"No events in the calendar for {span}.")

    async def test_search_gives_upcoming_and_the_latest_past_one(self):
        self.events = [FakeEvent("Dentist", "2026-11-01 09:00", "2026-11-01 10:00"),
                       FakeEvent("Dentist check", "2026-11-05 09:00", "2026-11-05 10:00"),
                       FakeEvent("Checkup", "2026-11-20 09:00", "2026-11-20 10:00", notes="at the dentist"),
                       FakeEvent("Gym", "2026-11-21 09:00", "2026-11-21 10:00")]
        self.assertEqual(await tools.find_calendar_events("DENTIST"),
                         "Upcoming calendar events matching 'DENTIST', soonest first (local time):\n"
                         "- Checkup: Friday 2026-11-20 09:00-10:00 (calendar Home)\n"
                         "Most recent past one: Dentist check: Thursday 2026-11-05 09:00-10:00 (calendar Home)")

    async def test_search_finds_nothing(self):
        self.assertEqual(await tools.find_calendar_events("dentist"),
                         "No calendar events matching 'dentist' between Monday 2026-10-12 and Thursday 2027-11-11.")

class AddTest(CalendarToolTest):
    SUMMARY = "Dentist: Friday 2026-11-13 14:30-15:30 (calendar Home)"

    async def add(self, date: str = "2026-11-13", start: str = "14:30", end_date: str | None = None,
                  end: str = "15:30") -> str:
        return await self.run_tool("add_calendar_event", {"title": "Dentist", "date": date, "start_time": start,
                                                           "end_date": end_date or date, "end_time": end})

    async def test_confirmed_event_is_saved_after_one_question(self):
        self.assertEqual(await self.add(), f"Added to the calendar: {self.SUMMARY}")
        self.assertEqual(self.questions, [f"Add this event to your calendar?\n\n{self.SUMMARY}"])
        saved = self.save.call_args.args[0]
        self.assertEqual((saved["title"], saved["start"], saved["end"], saved["all_day"]),
                         ("Dentist", local("2026-11-13 14:30"), local("2026-11-13 15:30"), False))

    async def test_without_an_end_it_lasts_one_hour(self):
        self.assertEqual(await self.run_tool("add_calendar_event", {"title": "Dentist", "date": "2026-11-13",
                                                                     "start_time": "14:30"}),
                         f"Added to the calendar: {self.SUMMARY}")

    async def test_empty_end_counts_as_no_end(self):
        self.assertEqual(await self.run_tool("add_calendar_event", {"title": "Dentist", "date": "2026-11-13",
                                                                     "start_time": "14:30", "end_date": "", "end_time": ""}),
                         f"Added to the calendar: {self.SUMMARY}")

    async def test_default_end_can_pass_midnight(self):
        await self.run_tool("add_calendar_event", {"title": "Dentist", "date": "2026-11-13", "start_time": "23:30"})
        self.assertEqual(self.save.call_args.args[0]["end"], local("2026-11-14 00:30"))

    async def test_end_time_without_end_date_is_on_the_same_day(self):
        await self.run_tool("add_calendar_event", {"title": "Dentist", "date": "2026-11-13", "start_time": "18:00",
                                                    "end_time": "20:00"})
        self.assertEqual(self.save.call_args.args[0]["end"], local("2026-11-13 20:00"))

    async def test_all_day_without_an_end_is_one_day(self):
        await self.run_tool("add_calendar_event", {"title": "Dentist", "date": "2026-11-13", "start_time": "all-day"})
        saved = self.save.call_args.args[0]
        self.assertEqual((saved["end"], saved["all_day"]), (local("2026-11-14 00:00"), True))

    async def test_timed_event_can_end_on_a_later_day(self):
        self.assertEqual(await self.add(end_date="2026-11-15", end="10:00"),
                         "Added to the calendar: Dentist: Friday 2026-11-13 14:30 to Sunday 2026-11-15 10:00 "
                         "(calendar Home)")
        saved = self.save.call_args.args[0]
        self.assertEqual((saved["start"], saved["end"], saved["all_day"]),
                         (local("2026-11-13 14:30"), local("2026-11-15 10:00"), False))

    async def test_all_day_event_over_several_days(self):
        self.assertEqual(await self.add(start="all-day", end_date="2026-11-15", end=""),
                         "Added to the calendar: Dentist: all day, Friday 2026-11-13 to Sunday 2026-11-15 "
                         "(calendar Home)")
        saved = self.save.call_args.args[0]
        self.assertEqual((saved["start"], saved["end"], saved["all_day"]),
                         (local("2026-11-13 00:00"), local("2026-11-16 00:00"), True))

    async def test_declined_is_not_saved(self):
        self.answer = False
        self.assertEqual(await self.add(), f"The user declined, so the event was NOT added: {self.SUMMARY}")
        self.save.assert_not_called()

    async def test_duplicate_is_not_asked_about_or_saved(self):
        self.events = [FakeEvent("dentist", "2026-11-13 14:30", "2026-11-13 15:00")]
        self.assertEqual(await self.add(), f"Not added, it's already in the calendar: {self.SUMMARY}")
        self.assertEqual(self.questions, [])
        self.save.assert_not_called()

    async def test_past_time_is_refused(self):
        self.assertEqual(await self.add("2026-11-11", "09:00"),
                         "Tool add_calendar_event failed: Wednesday 2026-11-11 09:00 is in the past; "
                         "today is Wednesday 2026-11-11")
        self.assertEqual(self.questions, [])

    async def test_all_day_today_is_allowed(self):
        self.assertEqual(await self.add("2026-11-11", "all-day", end=""),
                         "Added to the calendar: Dentist: Wednesday 2026-11-11, all day (calendar Home)")

    async def test_bad_start_time_names_the_accepted_forms(self):
        self.assertEqual(await self.add(start="noon"),
                         "Tool add_calendar_event failed: Start time 'noon' must be HH:MM (24-hour) or \"all-day\"")

    async def test_bad_end_time_names_the_accepted_form(self):
        self.assertEqual(await self.add(end="later"),
                         "Tool add_calendar_event failed: End time 'later' must be HH:MM (24-hour)")

    async def test_end_before_start_is_refused(self):
        self.assertEqual(await self.add(start="23:00", end="01:00"),
                         "Tool add_calendar_event failed: The event would end at Friday 2026-11-13 01:00, not after "
                         "its start at Friday 2026-11-13 23:00; one that runs past midnight needs a later end_date")
        self.assertEqual(self.questions, [])

    async def test_past_midnight_with_a_later_end_date(self):
        self.assertEqual(await self.add(start="23:00", end_date="2026-11-14", end="01:00"),
                         "Added to the calendar: Dentist: Friday 2026-11-13 23:00 to Saturday 2026-11-14 01:00 "
                         "(calendar Home)")

    async def test_all_day_ending_before_it_starts_is_refused(self):
        self.assertEqual(await self.add(start="all-day", end_date="2026-11-12", end=""),
                         "Tool add_calendar_event failed: The event's last day Thursday 2026-11-12 is before "
                         "its first day Friday 2026-11-13")

if __name__ == "__main__":
    unittest.main()
