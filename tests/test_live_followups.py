"""Follow-ups that act on an earlier answer, through the real router and model.

Skipped unless the chat server is running. Tools are faked, so nothing reaches the calendar.
"""

import unittest
from unittest import mock

from agent import conversation, tool_router
from agent.conversation import Conversation, Failed, Text
from frozen_clock import freeze
from model import mlx_server

EPISODE_QUESTION = "When next episode of ranma 1/2 season3 is airing?"
EPISODE_ANSWER = ("The next episode of Ranma ½ Season 3 (Episode 7) is airing this Saturday, "
                  "November 14, 2026, at 6:00 PM local time.")
ASKED_FOR_DETAILS = ("You haven't specified the details for the event. Please provide the following:\n"
                     "- Title (e.g., \"Ranma ½ S3 E7\")\n- Date (e.g., \"2026-11-14\")\n"
                     "- Start Time (e.g., \"18:00\")\n- End Date/Time (e.g., \"2026-11-14 20:00\")")

def turn(question: str, answer: str) -> list[dict]:
    return [{"role": "system", "content": "Current local time: 19:30."},
            {"role": "user", "content": question}, {"role": "assistant", "content": answer}]

class LiveFollowUpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not mlx_server.chat_is_ready():
            self.skipTest("the chat server isn't running")
        freeze(self, "2026-11-08 19:30")
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)
        self.calls = []
        async def run_tool(name, arguments):
            self.calls.append((name, arguments))
            return "Added to the calendar."
        patcher = mock.patch.object(conversation, "run_tool", run_tool)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def ask(self, history: list[dict], question: str) -> str:
        chat = Conversation()
        chat.history = list(history)
        events = [event async for event in chat.stream_reply(question)]
        self.assertEqual([e for e in events if isinstance(e, Failed)], [])
        return "".join(e.text for e in events if isinstance(e, Text))

    def assert_event_added(self, reply: str) -> None:
        added = [arguments for name, arguments in self.calls if name == "add_calendar_event"]
        self.assertTrue(added, f"add_calendar_event wasn't called; reply: {reply!r}")
        self.assertEqual(added[0]["date"], "2026-11-14")
        self.assertEqual(added[0]["start_time"], "18:00")

    async def test_add_this_event_uses_the_earlier_answer(self):
        self.assert_event_added(await self.ask(turn(EPISODE_QUESTION, EPISODE_ANSWER), "Add this event to calendar"))

    async def test_bare_times_finish_the_event(self):
        history = turn(EPISODE_QUESTION, EPISODE_ANSWER) + turn("Add this event to calendar", ASKED_FOR_DETAILS)
        self.assert_event_added(await self.ask(history, "18 to 20"))

if __name__ == "__main__":
    unittest.main()
