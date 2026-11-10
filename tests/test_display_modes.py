"""Default and Detailed mode (shift+t) and the spinner's labels during a turn, with the model faked."""

import asyncio
import unittest
from unittest import mock

from textual.widgets import Input, Markdown, Static

from core.config import config
from model import mlx_client, mlx_server
from test_conversation import FakeClient, call, chunk, think
from ui.app import DecartesApp
from ui.chat import Chat, Reply, Spinner


class DisplayModeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, name, value in [(mlx_server, "ensure_chat", mock.Mock()), (config, "model", "some/model"),
                                    (config, "tool_routing", False), (config, "compact_at", None),
                                    (config, "detailed", False)]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use(self, *rounds) -> None:
        client = FakeClient(*rounds)
        patcher = mock.patch.object(mlx_client, "stream_chat", client.stream_chat)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def ask(self, app, pilot, question="q") -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.query_one(Input).focus()
        app.query_one(Input).value = question
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def mode_label(self, app) -> str:
        return str(app.query_one("#mode", Static).render())

    async def test_thinking_and_usage_show_only_in_detailed_mode(self):
        self.use([think("Let me "), think("think."), chunk("The answer.", done=True)])
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ask(app, pilot)
            reply = app.query_one(Reply)
            thinking, usage = reply.query_one(".thinking", Static), app.query_one(".usage", Static)
            self.assertEqual(str(thinking.render()), "Let me think.")
            self.assertEqual(self.mode_label(app), "Default · shift+t")
            self.assertFalse(thinking.display or usage.display)
            self.assertEqual(app.query_one("#response").border_title, "Output")

            app.screen.set_focus(None)
            await pilot.press("T")
            await pilot.pause()
            self.assertTrue(config.detailed)
            self.assertEqual(self.mode_label(app), "Detailed · shift+t")
            self.assertTrue(app.query_one("#mode").has_class("detailed"))
            self.assertTrue(thinking.display and usage.display)
            self.assertEqual(app.query_one("#response").border_title, "Output · 15 tokens")

            await pilot.press("shift+t")
            await pilot.pause()
            self.assertFalse(config.detailed)
            self.assertFalse(thinking.display)

    async def test_thinking_and_answer_keep_their_order_across_tool_rounds(self):
        self.use([think("Need a tool."), chunk(tool_calls=[call("get_current_season")], done=True)],
                 [think("Got it."), chunk("Fall.", done=True)])
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ask(app, pilot)
            parts = list(app.query_one(Reply).children)
            self.assertEqual([type(part) for part in parts], [Static, Static, Markdown])
            self.assertEqual([str(part.render()) for part in parts[:2]], ["Need a tool.", "Got it."])

    async def test_typing_a_capital_t_does_not_switch_modes(self):
        self.use()
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.query_one(Input).focus()
            await pilot.press("T")
            self.assertEqual(app.query_one(Input).value, "T")
            self.assertFalse(config.detailed)

class SpinnerLabelTest(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_then_answering_then_tools_then_thinking_again(self):
        labels = []
        release = asyncio.Event()

        async def stream_chat(messages, tools=None):
            round_number = len(labels_by_round)
            labels_by_round.append(None)
            if round_number == 0:
                yield think("hmm")
                labels.append(spinner.label)
                yield chunk("Checking.")
                await asyncio.sleep(0.05)
                labels.append(spinner.label)
                yield chunk(tool_calls=[call("get_current_season")], done=True)
            else:
                await release.wait()
                labels.append(spinner.label)
                yield chunk("Done.", done=True)

        labels_by_round = []
        with mock.patch.object(mlx_server, "ensure_chat", mock.Mock()), \
             mock.patch.object(config, "tool_routing", False), mock.patch.object(config, "compact_at", None), \
             mock.patch.object(Spinner, "MIN_TOOL_SECONDS", 0.05), \
             mock.patch.object(mlx_client, "stream_chat", stream_chat):
            app = DecartesApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                spinner = app.query_one(Spinner)
                app.query_one(Chat).set_busy(True)
                app.query_one(Chat).fetch_stream("q")
                await pilot.pause(0.3)
                release.set()
                await app.workers.wait_for_complete()
        self.assertEqual(labels, ["Thinking...", "Answering...", "Thinking..."])
