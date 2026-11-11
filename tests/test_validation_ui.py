"""The /validate command in the app, with the model, embeddings and model server faked."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Input, Markdown, SelectionList

from agent import tool_router
from core.config import config
from model import mlx_client, mlx_server
from temp_history import temp_history
from test_conversation import FakeClient, call, chunk
from test_tool_router import FakeEmbeddings
from test_validation import case_data, suite_data
from ui.app import DecartesApp
from ui.chat import Chat, Spinner
from ui.config_screen import ConfigScreen
from validation import validation
from validation.validation_screen import ValidationScreen

ANSWER = [[chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("CPU is fine.", done=True)]]

class ValidateCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_history(self)
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.suite = validation.parse_suite(suite_data(
            case_data(id="1", question="one"), case_data(id="2", question="two", category="paraphrased")))
        self.client = FakeClient(*ANSWER, *ANSWER)
        for target, name, value in [
                (mlx_server, "ensure_chat", mock.Mock()), (config, "model", "org/some-model"),
                (validation, "RESULTS_FOLDER", self.folder), (validation, "load_suite", mock.Mock(return_value=self.suite)),
                (mlx_client, "stream_chat", self.client.stream_chat), (mlx_client, "complete", self.client.complete),
                (mlx_client, "embed", FakeEmbeddings({"one": {"server": 0.9}, "two": {"server": 0.9}}))]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def settle(self, app, pilot) -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def open_screen(self, app, pilot) -> None:
        await self.settle(app, pilot)
        app.query_one(Input).focus()
        app.query_one(Input).value = "/validate"
        await pilot.press("enter")
        await pilot.pause()

    def saved_model_results(self) -> dict:
        files = [path for path in self.folder.iterdir() if not path.name.endswith("-router.json")]
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    async def test_the_screen_offers_the_categories_in_the_file_all_ticked(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            self.assertIsInstance(app.screen, ValidationScreen)
            self.assertEqual(app.screen.query_one(SelectionList).selected, ["explicit", "paraphrased"])

    async def test_running_all_shows_the_report_and_saves_without_touching_the_conversation(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            await pilot.press("r")
            await self.settle(app, pilot)
            chat = app.query_one(Chat)
            self.assertIn("org/some-model", app.query_one(Markdown).source)
            self.assertIn("Full score 100.0", app.query_one(Markdown).source)
            self.assertEqual(chat.conversation.history, [])
            self.assertFalse(chat.busy)
        self.assertEqual(self.saved_model_results()["status"], "complete")

    async def test_an_unticked_category_is_skipped_and_the_run_is_partial(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            app.screen.query_one(SelectionList).deselect("paraphrased")
            await pilot.press("r")
            await self.settle(app, pilot)
        saved = self.saved_model_results()
        self.assertEqual(saved["status"], "partial")
        self.assertEqual([c["id"] for c in saved["cases"]], ["1"])

    async def test_nothing_ticked_stays_on_the_screen(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            app.screen.query_one(SelectionList).deselect_all()
            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, ValidationScreen)
            self.assertFalse(app.query_one(Chat).busy)

    async def test_escape_runs_nothing(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertEqual(self.client.sent, [])
            self.assertFalse(app.query_one(Chat).busy)

    async def test_a_broken_case_file_is_shown_and_no_screen_opens(self):
        validation.load_suite.side_effect = ValueError("validation case 'x': unknown tool 'nope'")
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            self.assertNotIsInstance(app.screen, ValidationScreen)
            self.assertIn("nope", [n.message for n in app._notifications][-1])

    async def hang_on_second_case(self) -> asyncio.Event:
        started, hang, stream = asyncio.Event(), asyncio.Event(), self.client.stream_chat
        async def hanging(messages, tools=None):
            if any(m["content"] == "two" for m in messages):
                started.set()
                await hang.wait()
            async for delta in stream(messages, tools):
                yield delta
        patcher = mock.patch.object(mlx_client, "stream_chat", hanging)
        patcher.start()
        self.addCleanup(patcher.stop)
        return started

    async def test_ctrl_c_stops_the_run_and_saves_what_finished_as_partial(self):
        started = await self.hang_on_second_case()
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            await pilot.press("r")
            await asyncio.wait_for(started.wait(), 5)
            await pilot.pause()
            self.assertEqual(app.query_one(Spinner).label, "Validating 1/2")
            await pilot.press("ctrl+c")
            await self.settle(app, pilot)
            self.assertFalse(app.query_one(Chat).busy)
            self.assertIn("partial", app.query_one(Markdown).source)
        self.assertEqual([c["id"] for c in self.saved_model_results()["cases"]], ["1"])

    async def test_settings_can_not_be_opened_during_a_run(self):
        started = await self.hang_on_second_case()
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.open_screen(app, pilot)
            await pilot.press("r")
            await asyncio.wait_for(started.wait(), 5)
            app.action_config()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfigScreen)
            await pilot.press("ctrl+c")
            await self.settle(app, pilot)

    async def test_a_results_file_that_cannot_be_written_still_shows_the_report(self):
        blocker = self.folder / "a file"
        blocker.write_text("")
        app = DecartesApp()
        with mock.patch.object(validation, "RESULTS_FOLDER", blocker / "results"):
            async with app.run_test(size=(100, 30)) as pilot:
                await self.open_screen(app, pilot)
                await pilot.press("r")
                await self.settle(app, pilot)
                self.assertIn("Full score", app.query_one(Markdown).source)
                self.assertTrue(any("Not a directory" in n.message for n in app._notifications))
