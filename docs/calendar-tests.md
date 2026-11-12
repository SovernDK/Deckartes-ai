# Testing the calendar tools

Calendar tests need a fake in place of EventKit. macOS gives calendar access to the app that runs Deckartes (Terminal, VS Code). A process that can't hold the permission, such as Claude Code's own shell, gets an instant refusal with the status still undetermined.

`tests/test_calendar_tools.py` calls the tools themselves and asserts the exact text they return. It replaces three functions in `calendar_api`:

| Faked | Stands in for | Notes |
| --- | --- | --- |
| `_events_between(start, end)` | Reading the event store | Returns `FakeEvent`s overlapping the range, soonest first |
| `_target_calendar()` | Choosing the calendar for new events | Returns an object with `title()` |
| `save_event(event)` | Writing to the event store | A `Mock`; assert on `call_args` or `assert_not_called` |

`FakeEvent` has the EventKit shape: `title()`, `location()`, `notes()`, `startDate()`, `endDate()`, `isAllDay()` and `calendar()`. Its dates are real `NSDate`s, so `_slim` and the time zone conversion run as they do in the app.

`add_calendar_event` asks the user through the `confirm` its turn passes. The tests' `run_tool` helper calls `tools.run_tool` with a `confirm` that records the question and returns the answer, covering both yes and no. `frozen_clock.freeze` fixes the time, which the past-date refusal and the relative days ("tomorrow", "next friday") depend on.

## Not covered

The fakes leave out `_event_store`, the body of `_events_between`, `_target_calendar` and `save_event`, the code that talks to EventKit. It needs a manual check in the running app, from a terminal with calendar access, after changes there.

## Why there is no plain-data seam

A seam that returned dicts in place of EventKit objects would need a production refactor, and with only EventKit behind it there would be one adapter. The three functions above are enough until a second calendar source exists.
