"""Real questions through the router and config.embed_model. Live: runs only with LIVE=1 and the chat
server up. Rerun after changing the embedding model, the group examples or the thresholds."""

import unittest
from unittest import mock

from agent import tool_router
from core.config import config
from live import require_live
from model import mlx_client
from test_tool_router import ask, use_preset

# The most groups a one-topic question may send; a fixed number, so that loosening the router fails
MAX_GROUPS = 2

# (previous questions..., question, groups that must be chosen; [] = no tools at all)
LIVE_CASES = [
    # server
    (("Is the thinkpad online right now?",), ["server"]),
    (("How warm is my server running?",), ["server"]),
    (("Are all my containers healthy?",), ["server"]),
    (("How much memory is free on the home server?",), ["server"]),
    # disk
    (("Show me what's in my Desktop folder",), ["disk"]),
    (("Where did I put my tax return pdf?",), ["disk"]),
    (("How much space does my Pictures folder take?",), ["disk"]),
    # web
    (("What's the latest news about the iPhone?",), ["web"]),
    (("Look up how tall the Eiffel Tower is online",), ["web"]),
    (("Read this page for me: https://example.com",), ["web"]),
    # anime
    (("Which anime are premiering this fall?",), ["anime"]),
    (("What's the highest rated show this season?",), ["anime"]),
    (("When does Dandadan air next?",), ["anime"]),
    (("Is Spy x Family getting another season?",), ["anime"]),
    (("Which shows from my list come out tomorrow?",), ["anime"]),
    # cs
    (("Who's ranked first in CS2 right now?",), ["cs"]),
    (("Did FaZe win yesterday?",), ["cs"]),
    (("Who plays for G2?",), ["cs"]),
    (("Any Counter-Strike games on right now?",), ["cs"]),
    # calendar
    (("Do I have anything planned this weekend?",), ["calendar"]),
    (("Am I busy on Tuesday afternoon?",), ["calendar"]),
    (("Add lunch with Tom on Wednesday at 13:00",), ["calendar"]),
    (("When was my last haircut?",), ["calendar"]),
    # two tools
    (("Put the next Dandadan episode in my calendar",), ["calendar", "anime"]),
    (("Add Vitality's next match to my calendar",), ["calendar", "cs"]),
    # no tools
    (("Hi there!",), []),
    (("Write a haiku about autumn",), []),
    (("What's 15% of 80?",), []),
    (("Explain what a linked list is",), []),
    (("Thanks a lot!",), []),
    # unknown names: the right group must be among the unsure picks
    (("Who is ropz?",), ["cs"]),
    (("Is Kaiju No. 8 any good?",), ["anime"]),
    # follow-ups
    (("When does MOUZ play next?", "and Spirit?"), ["cs"]),
    (("Which anime come out today?", "and tomorrow?"), ["anime"]),
    (("Is Vitality playing today?", "and tomorrow?"), ["cs"]),
    (("What do I have on today?", "what about Friday?"), ["calendar"]),
    (("Who won the last CS major?", "thanks!"), []),
    # answers to something the model asked
    (("Add this event to calendar", "18 to 20"), ["calendar"]),
    (("Add the next Frieren episode to my calendar", "yes"), ["calendar"]),
]


class LiveRoutingTest(unittest.IsolatedAsyncioTestCase):
    """Real questions through config.embed_model. Rerun after changing the embedding model,
    the group examples or the thresholds."""

    async def asyncSetUp(self):
        require_live(self)
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)

    async def scores(self, question: str) -> str:
        examples = await tool_router._example_vectors()
        vector = tool_router._unit(await mlx_client.embed([question]))[0]
        scores = tool_router._scores(vector, examples)
        return ", ".join(
            f"{name or 'no tools'} {score:.2f}" for name, score in sorted(scores.items(), key=lambda s: -s[1])
        )

    async def test_questions_reach_their_groups(self):
        use_preset(self, "all")
        with mock.patch.object(config, "tool_routing", True):
            for questions, expected in LIVE_CASES:
                with self.subTest(questions[-1]):
                    groups = (await tool_router.select_tools(*ask(*questions))).groups or []
                    detail = f"chose {groups}; scores: {await self.scores(questions[-1])}"
                    if expected:
                        self.assertLessEqual(set(expected), set(groups), detail)
                        self.assertLessEqual(len(groups), max(len(expected), MAX_GROUPS), detail)
                    else:
                        self.assertEqual(groups, [], detail)


if __name__ == "__main__":
    unittest.main()
