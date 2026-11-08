"""Apple Calendar through EventKit: read events and add new ones.

Sees every calendar synced to this Mac (iCloud, Google, Exchange...). The first call asks macOS
for calendar access. The permission belongs to the app running Deckartes (Terminal, iTerm,
VS Code) and can be changed in System Settings > Privacy & Security > Calendars.

Times are in config.timezone. EventKit is blocking, so the public functions at the bottom run
the private ones in a thread and return tool text.
"""

import asyncio
import datetime as dt
import threading
from collections.abc import Awaitable, Callable

import EventKit
from Foundation import NSDate

import clock
from config import config

# How far find_events looks back and ahead
SEARCH_PAST_DAYS = 30
SEARCH_FUTURE_DAYS = 365
MAX_EVENTS = 60
MAX_FOUND = 10
ACCESS_TIMEOUT_SECONDS = 120
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_store = None
_store_lock = threading.Lock()

def _request_access(store) -> bool:
    """Show macOS's calendar permission prompt and wait for the answer."""
    done = threading.Event()
    answer = {}
    def handler(granted, error):
        answer["granted"] = bool(granted)
        done.set()
    store.requestFullAccessToEventsWithCompletion_(handler)
    if not done.wait(ACCESS_TIMEOUT_SECONDS):
        raise TimeoutError("no answer to the macOS calendar permission prompt")
    return answer["granted"]

def _event_store():
    """The shared EKEventStore, asking for calendar access the first time."""
    global _store
    with _store_lock:
        if _store is None:
            store = EventKit.EKEventStore.alloc().init()
            status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
            if status == EventKit.EKAuthorizationStatusNotDetermined:
                if not _request_access(store):
                    # Still undetermined: macOS refused without asking, which happens when the
                    # parent process isn't an app that can hold the permission
                    if (EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
                            == EventKit.EKAuthorizationStatusNotDetermined):
                        raise PermissionError("macOS refused calendar access without asking; run Deckartes "
                                              "from a terminal app such as Terminal, iTerm or VS Code")
                    raise PermissionError("calendar access was denied")
                # A store made before access was granted doesn't see the calendars
                store = EventKit.EKEventStore.alloc().init()
            elif status != EventKit.EKAuthorizationStatusFullAccess:
                raise PermissionError("no access to the calendar; allow it for this terminal app in "
                                      "System Settings > Privacy & Security > Calendars")
            _store = store
    return _store

def _nsdate(when: dt.datetime) -> NSDate:
    return NSDate.dateWithTimeIntervalSince1970_(when.timestamp())

def _local(nsdate) -> dt.datetime:
    return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970(), clock.zone())

def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=clock.zone())

def _parse_day(text: str) -> dt.date:
    """"today", "tomorrow", "yesterday", a weekday ("friday" is the next one, today included;
    "next friday" is after today) or YYYY-MM-DD."""
    key = text.strip().lower()
    now = clock.today()
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

def _slim(event) -> dict:
    item = {
        "title": event.title() or "(no title)",
        "start": _local(event.startDate()),
        "end": _local(event.endDate()),
        "all_day": bool(event.isAllDay()),
        "calendar": event.calendar().title() if event.calendar() else None,
    }
    if event.location():
        item["location"] = event.location().replace("\n", ", ")
    return item

def _events_between(start: dt.datetime, end: dt.datetime) -> list:
    """EKEvents overlapping start..end in every calendar, soonest first; recurring ones expanded."""
    store = _event_store()
    # The store only hears about changes made in Calendar.app through notifications on a macOS
    # run loop, which this app doesn't run, so drop its cache to see events added since
    store.reset()
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(_nsdate(start), _nsdate(end), None)
    events = list(store.eventsMatchingPredicate_(predicate) or [])
    return sorted(events, key=lambda e: e.startDate().timeIntervalSince1970())

def _events_on(day: dt.date) -> dict:
    events = _events_between(_midnight(day), _midnight(day + dt.timedelta(days=1)))
    return {"first": day, "last": day, "events": [_slim(e) for e in events[:MAX_EVENTS]]}

def _month_end(first: dt.date) -> dt.date:
    next_first = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return next_first - dt.timedelta(days=1)

def _period(period: str) -> tuple[dt.date, dt.date, str]:
    """First and last day of a period, and how to name it. "this week" / "this month" start today;
    a month name is the next one (this month counts, from today)."""
    now = clock.today()
    key = " ".join(period.strip().lower().split())
    if key in ("this week", "week"):
        return now, now + dt.timedelta(days=6 - now.weekday()), "the rest of this week"
    if key == "next week":
        first = now + dt.timedelta(days=7 - now.weekday())
        return first, first + dt.timedelta(days=6), "next week"
    if key in ("this month", "month"):
        return now, _month_end(now), "the rest of this month"
    if key == "next month":
        first = _month_end(now) + dt.timedelta(days=1)
        return first, _month_end(first), "next month"
    if key in MONTHS:
        month = MONTHS.index(key) + 1
        year = now.year if month >= now.month else now.year + 1
        first = max(now, dt.date(year, month, 1))
        return first, _month_end(first), f"{key.title()} {year}"
    try:
        first = dt.date.fromisoformat(f"{key}-01")
    except ValueError:
        raise ValueError(f"Period {period!r} must be \"this week\", \"next week\", \"this month\", "
                         f"\"next month\", a month name or YYYY-MM")
    return first, _month_end(first), f"{first:%B %Y}"

def _period_events(period: str) -> dict:
    first, last, name = _period(period)
    events = _events_between(_midnight(first), _midnight(last + dt.timedelta(days=1)))
    return {"first": first, "last": last, "name": name, "events": [_slim(e) for e in events[:MAX_EVENTS]]}

def _find_events(query: str) -> dict:
    """Events whose title, location or notes contain query: the next MAX_FOUND and the latest past one."""
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("Give a word to search for")
    now = clock.now()
    events = _events_between(now - dt.timedelta(days=SEARCH_PAST_DAYS), now + dt.timedelta(days=SEARCH_FUTURE_DAYS))
    matches = [_slim(e) for e in events
               if any(needle in (text or "").casefold() for text in (e.title(), e.location(), e.notes()))]
    upcoming = [e for e in matches if e["end"] > now]
    past = [e for e in matches if e["end"] <= now]
    return {"query": query, "upcoming": upcoming[:MAX_FOUND], "last_past": past[-1] if past else None,
            "from": (now - dt.timedelta(days=SEARCH_PAST_DAYS)).date(),
            "to": (now + dt.timedelta(days=SEARCH_FUTURE_DAYS)).date()}

def _target_calendar():
    """The calendar new events go to: config.calendar_name, or Calendar's default."""
    store = _event_store()
    if not config.calendar_name:
        calendar = store.defaultCalendarForNewEvents()
        if calendar is None:
            raise ValueError("there is no default calendar for new events; set config.calendar_name")
        return calendar
    writable = [c for c in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) if c.allowsContentModifications()]
    for calendar in writable:
        if calendar.title().casefold() == config.calendar_name.casefold():
            return calendar
    names = ", ".join(c.title() for c in writable)
    raise ValueError(f"no calendar named {config.calendar_name!r}; calendars you can add to: {names}")

def _new_event(title: str, date: str, start_time: str, end_date: str, end_time: str) -> dict:
    """Check and complete an event to add, without saving it. end_date is the event's last day."""
    title = title.strip()
    if not title:
        raise ValueError("The event needs a title")
    day = _parse_day(date)
    last = _parse_day(end_date)
    if start_time.strip().lower() in ("all-day", "all day", "allday"):
        if last < day:
            raise ValueError(f"The event's last day {_day(last)} is before its first day {_day(day)}")
        start, end, all_day = _midnight(day), _midnight(last + dt.timedelta(days=1)), True
    else:
        try:
            at = dt.time.fromisoformat(start_time.strip())
        except ValueError:
            raise ValueError(f"Start time {start_time!r} must be HH:MM (24-hour) or \"all-day\"")
        try:
            until = dt.time.fromisoformat(end_time.strip())
        except ValueError:
            raise ValueError(f"End time {end_time!r} must be HH:MM (24-hour)")
        start = dt.datetime.combine(day, at, tzinfo=clock.zone())
        end = dt.datetime.combine(last, until, tzinfo=clock.zone())
        all_day = False
        if end <= start:
            raise ValueError(f"The event would end at {end:%A %Y-%m-%d %H:%M}, not after its start at "
                             f"{start:%A %Y-%m-%d %H:%M}; one that runs past midnight needs a later end_date")
    # A past date is almost always a wrong year or day from the model
    if (end if all_day else start) <= clock.now():
        raise ValueError(f"{start:%A %Y-%m-%d %H:%M} is in the past; today is {clock.today():%A %Y-%m-%d}")
    return {"title": title, "start": start, "end": end, "all_day": all_day,
            "calendar": _target_calendar().title()}

def _is_duplicate(event: dict) -> bool:
    """Whether an event with the same title already starts at the same time."""
    existing = _events_between(event["start"], event["end"])
    return any(_slim(e)["title"].casefold() == event["title"].casefold() and _slim(e)["start"] == event["start"]
               for e in existing)

def save_event(event: dict) -> None:
    store = _event_store()
    ek_event = EventKit.EKEvent.eventWithEventStore_(store)
    ek_event.setTitle_(event["title"])
    ek_event.setStartDate_(_nsdate(event["start"]))
    ek_event.setEndDate_(_nsdate(event["end"]))
    ek_event.setAllDay_(event["all_day"])
    ek_event.setCalendar_(_target_calendar())
    saved, error = store.saveEvent_span_error_(ek_event, EventKit.EKSpanThisEvent, None)
    if not saved:
        raise RuntimeError(error.localizedDescription() if error else "Calendar refused to save the event")

# Text for the tools: the event name first, then when, then where

def _day(day: dt.date) -> str:
    return f"{day:%A %Y-%m-%d}"

def _when(event: dict, with_date: bool) -> str:
    start, end = event["start"], event["end"]
    if event["all_day"]:
        last = (end - dt.timedelta(seconds=1)).date()
        when = "all day" if last == start.date() else f"all day, {_day(start.date())} to {_day(last)}"
        return f"{_day(start.date())}, {when}" if with_date and last == start.date() else when
    if end.date() == start.date():
        span = f"{start:%H:%M}-{end:%H:%M}"
    else:
        span = f"{start:%H:%M} to {_day(end.date())} {end:%H:%M}"
    return f"{_day(start.date())} {span}" if with_date else span

def _last_day(event: dict) -> dt.date:
    # An event ending at midnight doesn't reach the day that starts then
    return max((event["end"] - dt.timedelta(seconds=1)).date(), event["start"].date())

def _event_line(event: dict, with_date: bool = False) -> str:
    about = [f"calendar {event['calendar']}"] if event.get("calendar") else []
    if event.get("location"):
        about.append(f"at {event['location']}")
    extra = f" ({'; '.join(about)})" if about else ""
    return f"{event['title']}: {_when(event, with_date)}{extra}"

def _events_text(result: dict) -> str:
    first, last, events = result["first"], result["last"], result["events"]
    span = _day(first) if first == last else f"{_day(first)} to {_day(last)}"
    if result.get("name"):
        span = f"{result['name']}, {span}"
    if not events:
        return f"No events in the calendar for {span}."
    note = "local time; days not listed have no events" if first != last else "local time"
    lines = [f"Calendar events for {span} ({note}):"]
    # A multi-day event is listed under every day it covers, so an unlisted day really is free,
    # and with its full dates, since the day heading isn't the day it started
    by_day: dict[dt.date, list[str]] = {}
    for event in events:
        multi_day = _last_day(event) != event["start"].date()
        day = max(event["start"].date(), first)
        while day <= min(_last_day(event), last):
            by_day.setdefault(day, []).append(f"- {_event_line(event, with_date=multi_day)}")
            day += dt.timedelta(days=1)
    for day in sorted(by_day):
        if first != last:
            lines.append(f"{_day(day)}:")
        lines += by_day[day]
    if len(events) == MAX_EVENTS:
        lines.append(f"(only the first {MAX_EVENTS} events are listed)")
    return "\n".join(lines)

def _found_text(result: dict) -> str:
    query = result["query"]
    if not result["upcoming"] and not result["last_past"]:
        return (f"No calendar events matching {query!r} between {_day(result['from'])} "
                f"and {_day(result['to'])}.")
    lines = []
    if result["upcoming"]:
        lines.append(f"Upcoming calendar events matching {query!r}, soonest first (local time):")
        lines += [f"- {_event_line(e, with_date=True)}" for e in result["upcoming"]]
    else:
        lines.append(f"No upcoming calendar events matching {query!r} (searched until {_day(result['to'])}).")
    if result["last_past"]:
        lines.append(f"Most recent past one: {_event_line(result['last_past'], with_date=True)}")
    return "\n".join(lines)

# For the tools

async def events_on(date: str) -> str:
    return _events_text(await asyncio.to_thread(_events_on, _parse_day(date)))

async def period_events(period: str) -> str:
    return _events_text(await asyncio.to_thread(_period_events, period))

async def find_events(query: str) -> str:
    return _found_text(await asyncio.to_thread(_find_events, query))

async def add_event(title: str, date: str, start_time: str, end_date: str, end_time: str,
                    confirm: Callable[[str], Awaitable[bool]]) -> str:
    """Add an event unless it's already there or the user, asked through confirm, declines."""
    event = await asyncio.to_thread(_new_event, title, date, start_time, end_date, end_time)
    summary = _event_line(event, with_date=True)
    if await asyncio.to_thread(_is_duplicate, event):
        return f"Not added, it's already in the calendar: {summary}"
    if not await confirm(f"Add this event to your calendar?\n\n{summary}"):
        return f"The user declined, so the event was NOT added: {summary}"
    await asyncio.to_thread(save_event, event)
    return f"Added to the calendar: {summary}"
