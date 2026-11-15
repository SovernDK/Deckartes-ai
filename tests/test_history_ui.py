"""The history panel and saving from the app, driven through the real app with only the model faked."""

import unittest
from unittest import mock

from textual.widgets import Input, ListView, Markdown, Static

from agent import history
from core.config import config
from model import mlx_client, mlx_server
from temp_history import save, temp_history
from test_conversation import FakeClient, chunk
from ui.app import DecartesApp
from ui.chat import Chat, ConfirmScreen
from ui.history_panel import HistoryPanel


class HistoryUiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = temp_history(self)
        for target, name, value in [
            (mlx_server, "ensure_chat", mock.Mock()),
            (config, "tool_routing", False),
            (config, "compact_at", None),
            (config, "detailed", False),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use(self, *rounds) -> FakeClient:
        client = FakeClient(*rounds)
        patcher = mock.patch.object(mlx_client, "stream_chat", client.stream_chat)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    async def settle(self, app, pilot) -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def ask(self, app, pilot, text: str) -> None:
        app.query_one(Input).focus()
        app.query_one(Input).value = text
        await pilot.press("enter")
        await self.settle(app, pilot)

    async def show_panel(self, app, pilot) -> HistoryPanel:
        app.screen.set_focus(None)
        await pilot.press("r")
        await self.settle(app, pilot)
        return app.query_one(HistoryPanel)

    def entries(self, app) -> list[str]:
        return [str(item.query_one(Static).render()) for item in app.query_one(HistoryPanel).query("ListItem")]

    def shown(self, app) -> list[str]:
        """The questions and answers on screen."""
        response = app.query_one("#response")
        return [
            widget.source if isinstance(widget, Markdown) else str(widget.render())
            for widget in response.query(".user-msg, .ai-msg, .reply Markdown")
        ]

    def messages(self, app) -> list[str]:
        return [notification.message for notification in app._notifications]

    async def test_r_shows_the_saved_conversations_newest_first_and_hides_them_again(self):
        save("Older question", "a", "2026-11-10 09:00", source="telegram")
        save("Newer question", "b", "2026-11-11 10:00")
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            panel = await self.show_panel(app, pilot)
            self.assertTrue(panel.display)
            self.assertEqual(
                self.entries(app), ["Newer question\n2026-11-11 10:00", "Older question\n2026-11-10 09:00 · Telegram"]
            )
            await pilot.press("r")
            await pilot.pause()
            self.assertFalse(panel.display)

    async def test_a_question_in_the_app_is_saved_and_listed(self):
        self.use([chunk("Hello!", done=True)])
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.ask(app, pilot, "hi")
            await self.show_panel(app, pilot)
            self.assertEqual([c.title for c in history.list_conversations()], ["hi"])
            self.assertEqual(len(self.entries(app)), 1)

    async def test_enter_opens_a_conversation_and_the_next_question_continues_it(self):
        save("Older question", "Old answer", "2026-11-10 09:00")
        client = self.use([chunk("New answer", done=True)])
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.show_panel(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertFalse(app.query_one(HistoryPanel).display)
            self.assertEqual(self.shown(app), ["Older question", "Old answer"])
            await self.ask(app, pilot, "Next question")
            self.assertIn("Old answer", [m["content"] for m in client.sent[0]["messages"]])
            [listed] = history.list_conversations()
            self.assertEqual(len(history.load(listed.id).messages), 6)

    async def test_a_conversation_cant_be_opened_while_the_model_is_answering(self):
        save("Older question", "Old answer", "2026-11-10 09:00")
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.show_panel(app, pilot)
            app.query_one(Chat).set_busy(True)
            app.query_one(ListView).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.shown(app), [])
            self.assertIn("Wait for the reply to finish before opening a conversation", self.messages(app))

    async def test_d_deletes_the_highlighted_conversation_after_confirming(self):
        save("Keep me", "a", "2026-11-10 09:00")
        save("Delete me", "b", "2026-11-11 10:00")
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.show_panel(app, pilot)
            await pilot.press("d")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            self.assertEqual(screen.question, "Delete 'Delete me'?")
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual([c.title for c in history.list_conversations()], ["Keep me"])
            self.assertEqual(self.entries(app), ["Keep me\n2026-11-10 09:00"])

    async def test_answering_no_keeps_the_conversation(self):
        save("Keep me", "a", "2026-11-10 09:00")
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.show_panel(app, pilot)
            await pilot.press("d", "n")
            await self.settle(app, pilot)
            self.assertEqual([c.title for c in history.list_conversations()], ["Keep me"])

    async def test_deleting_the_open_conversation_clears_the_chat(self):
        save("Open me", "a", "2026-11-10 09:00")
        client = self.use([chunk("Fresh", done=True)])
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.show_panel(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            await self.show_panel(app, pilot)
            await pilot.press("d", "y")
            await self.settle(app, pilot)
            self.assertEqual(self.shown(app), [])
            await self.ask(app, pilot, "new")
            self.assertNotIn("a", [m["content"] for m in client.sent[0]["messages"]])
            self.assertEqual([c.title for c in history.list_conversations()], ["new"])

    async def test_a_turn_that_cant_be_saved_shows_a_warning(self):
        (self.folder / "a file").write_text("")
        patcher = mock.patch.object(config, "history_path", self.folder / "a file" / "history.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.use([chunk("Hello!", done=True)])
        app = DecartesApp()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(app, pilot)
            await self.ask(app, pilot, "hi")
            self.assertEqual(self.shown(app), ["hi", "Hello!"])
            self.assertTrue(any(m.startswith("Couldn't save to history: ") for m in self.messages(app)))
