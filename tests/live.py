"""The opt-in for live tests (test_live_*.py), which use the real Rapid-MLX server: slow, and they
depend on the model, so a normal run skips them even when the server is up."""

import os
import unittest

from model import mlx_server


def require_live(test: unittest.TestCase) -> None:
    """Skip the test unless LIVE=1 is set and the chat server is running."""
    if os.environ.get("LIVE") != "1":
        test.skipTest("live test: run with LIVE=1")
    if not mlx_server.chat_is_ready():
        test.skipTest("the chat server isn't running")
