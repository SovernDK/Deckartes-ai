"""Points the saved conversations at a temporary file for one test, so no test touches the real history,
and saves conversations into it the way the app does."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import history
from core import clock
from core.config import config


def temp_history(test: unittest.TestCase) -> Path:
    """Use an empty history in a temporary folder until the test ends; returns the folder."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(config, "history_path", Path(folder.name) / "data" / "history.db")
    patcher.start()
    test.addCleanup(patcher.stop)
    return Path(folder.name)

def save(question: str, answer: str, at: str, source: str = "app", saver: history.Saver | None = None) -> history.Saver:
    """Save a finished turn at `at` ("YYYY-MM-DD HH:MM"), to a new conversation or to saver's; returns the saver."""
    saver = saver or history.Saver(source)
    moment = dt.datetime.fromisoformat(at)
    with clock.frozen_at(moment):
        saver.turn("Current local time: 12:00.", question, answer, moment, "")
    return saver

def save_summary(saver: history.Saver, summary: str, at: str) -> None:
    with clock.frozen_at(dt.datetime.fromisoformat(at)):
        saver.summary(summary)
