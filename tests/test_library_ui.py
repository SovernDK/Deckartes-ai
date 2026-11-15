"""Loading into the library from the explorer, and /library and /forget, driven through the real app."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Markdown

from core.config import config
from model import mlx_client, mlx_server
from temp_history import temp_history
from tools import library
from ui.app import DecartesApp
from ui.chat import Chat, ConfirmScreen
from ui.file_explorer import FileExplorerScreen, UserDirectoryTree


def embed(texts):
    return [[1.0, 0.0] for _ in texts]


class LibraryUiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_history(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        for target, name, value in [
            (mlx_server, "ensure_chat", mock.Mock()),
            (config, "library_path", self.folder / "data" / "library.db"),
            (mlx_client, "embed_file_text", embed),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.docs = self.folder / "docs"
        (self.docs / "deep").mkdir(parents=True)
        (self.docs / "a.txt").write_text("first file")
        (self.docs / "deep" / "b.md").write_text("second file")

    async def settle(self, app, pilot) -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def messages(self, app) -> list[str]:
        return [notification.message for notification in app._notifications]

    def saved(self) -> list[str]:
        return [entry.path.name for entry in library.entries()]

    async def open_explorer(self, app, pilot) -> UserDirectoryTree:
        app.push_screen(FileExplorerScreen(self.docs), app.file_chosen)
        await pilot.pause()
        tree = app.screen.query_one(UserDirectoryTree)
        tree.root.expand()
        # The tree's loader worker runs for as long as the tree does, so wait for its result instead
        for _ in range(100):
            if tree.last_line >= 2:
                break
            await pilot.pause(0.02)
        return tree

    async def test_l_saves_the_highlighted_folder(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            tree = await self.open_explorer(app, pilot)
            tree.cursor_line = 0
            await pilot.press("l")
            await self.settle(app, pilot)
            self.assertEqual(self.saved(), ["a.txt", "b.md"])
            self.assertEqual(self.messages(app), ["Saved 2 files (2 chunks) to the library."])
            self.assertFalse(app.query_one(Chat).busy)

    async def test_enter_on_a_file_saves_it(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            tree = await self.open_explorer(app, pilot)
            nodes = [tree.get_node_at_line(i) for i in range(tree.last_line + 1)]
            line = next(i for i, node in enumerate(nodes) if node and node.data and node.data.path.name == "a.txt")
            tree.cursor_line = line
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(self.saved(), ["a.txt"])

    async def test_a_file_that_cannot_be_read_is_reported(self):
        (self.docs / "bad.txt").write_bytes(b"\0binary")
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).load_file(self.docs / "bad.txt")
            await self.settle(app, pilot)
            self.assertEqual(
                self.messages(app), ["this looks like a binary file; only text files and PDFs can be loaded"]
            )
            self.assertEqual(self.saved(), [])

    async def test_library_command_shows_the_saved_files_outside_the_conversation(self):
        await library.add(self.docs / "a.txt")
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            chat = app.query_one(Chat)
            chat.run_command("library")
            await self.settle(app, pilot)
            self.assertIn("**Library:** 1 file, 1 chunk", app.query(Markdown).last().source)
            self.assertEqual(chat.conversation.history, [])

    async def test_forget_removes_after_a_yes(self):
        await library.add(self.docs)
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).run_command(f"forget {self.docs / 'deep'}")
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            self.assertIn("Remove 1 file from the library", screen.question)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(self.saved(), ["a.txt"])
            self.assertEqual(self.messages(app), ["Removed 1 file from the library"])

    async def test_forget_keeps_the_file_after_a_no(self):
        await library.add(self.docs)
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).run_command("forget a.txt")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("n")
            await self.settle(app, pilot)
            self.assertEqual(self.saved(), ["a.txt", "b.md"])

    async def test_forget_with_no_match_says_so(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).run_command("forget nothing.txt")
            await self.settle(app, pilot)
            self.assertEqual(self.messages(app), ["Nothing in the library matches nothing.txt"])

    async def test_forget_without_a_name_explains_itself(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).run_command("forget")
            await self.settle(app, pilot)
            self.assertEqual(self.messages(app), ["Usage: /forget <path or file name>"])


if __name__ == "__main__":
    unittest.main()
