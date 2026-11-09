"""Whole turns through the real Rapid-MLX server. Skipped unless the chat server is already running.

The library test starts the real file embedding server, and stops it again.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import library
import mlx_server
import tool_router
from config import config
from conversation import Conversation, Failed, Text, ToolStarted


class LiveTurnTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not mlx_server.chat_is_ready():
            self.skipTest("the chat server isn't running")
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)
        self.conversation = Conversation()

    async def turn(self, question: str) -> list:
        events = [event async for event in self.conversation.stream_reply(question)]
        self.assertEqual([e for e in events if isinstance(e, Failed)], [])
        return events

    def text(self, events: list) -> str:
        return "".join(e.text for e in events if isinstance(e, Text))

    async def test_plain_question_streams_and_reports_speed(self):
        events = await self.turn("What is the capital of Poland? One short sentence.")
        self.assertIn("Warsaw", self.text(events))
        self.assertNotIn("<think>", self.text(events))
        usage = self.conversation.last_turn.usage
        self.assertGreater(usage.input, 0)
        self.assertGreater(usage.tokens_per_second, 1)

    async def test_tool_round_ends_in_an_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            events = await self.turn(f"How big is the folder {folder} on my disk?")
        self.assertIn(ToolStarted("get_folder_size"), events)
        self.assertTrue(self.text(events).strip())

    async def test_second_turn_keeps_the_conversation(self):
        await self.turn("My cat is called Mruczek. Just say OK.")
        events = await self.turn("What is my cat called?")
        self.assertIn("Mruczek", self.text(events))

    async def test_saved_file_is_searched_through_the_second_server(self):
        self.addCleanup(mlx_server.stop_all)
        filler = "\n\n".join(f"Akapit {i} opowiada o pogodzie, drogach i zwyklych sprawach w miescie numer {i}. " * 12
                             for i in range(12))
        secret = "Kod do sejfu w piwnicy to 4417."
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(config, "library_path", Path(folder) / "library.db"):
            path = Path(folder) / "notatki.txt"
            path.write_text(f"{filler}\n\n{secret}\n\n{filler}")
            await library.add(path)
            events = await self.turn("Sprawdz w moich zapisanych plikach: jaki jest kod do sejfu w piwnicy?")
        self.assertIn(ToolStarted("search_library"), events)
        self.assertIn("4417", self.text(events))

if __name__ == "__main__":
    unittest.main()
