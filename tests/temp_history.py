"""Points the saved conversations at a temporary file for one test, so no test touches the real history."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.config import config


def temp_history(test: unittest.TestCase) -> Path:
    """Use an empty history in a temporary folder until the test ends; returns the folder."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(config, "history_path", Path(folder.name) / "data" / "history.db")
    patcher.start()
    test.addCleanup(patcher.stop)
    return Path(folder.name)
