"""The list of /command suggestions above the input."""

import unittest
from unittest import mock

from textual.widgets import Input, OptionList

from model import mlx_server
from temp_history import temp_history
from ui.app import DecartesApp
from ui.chat import Chat


class CommandSuggestionsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_history(self)
        patcher = mock.patch.object(mlx_server, "ensure_chat", mock.Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matching(self):
        self.assertEqual(Chat.matching_commands("/"), list(Chat.COMMANDS))
        self.assertEqual(Chat.matching_commands("/VAL"), ["validate"])
        self.assertEqual(Chat.matching_commands("/ib"), ["library"])  # contains, not a prefix
        self.assertEqual(Chat.matching_commands("hello"), [])
        self.assertEqual(Chat.matching_commands("/forget file"), [])

    async def test_typing_filters_and_tab_completes(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            box, shown = app.query_one(Input), app.query_one("#suggestions", OptionList)
            box.focus()
            await pilot.press("/", "k")
            await pilot.pause()
            self.assertTrue(shown.display)
            self.assertEqual([o.id for o in shown.options], ["kill"])
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(box.value, "/kill")
            box.value = "hi"
            await pilot.pause()
            self.assertFalse(shown.display)

    async def test_enter_completes_a_partial_command_and_adds_space_for_arguments(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            box = app.query_one(Input)
            box.focus()
            box.value = "/fo"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(box.value, "/forget ")
            self.assertFalse(app.query_one("#suggestions").display)
