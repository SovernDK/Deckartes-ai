"""Tests for tool_router.select_tools.

The rule tests swap tool_router._embed for fake vectors, so each question gets exactly the
similarity scores a test asks for. The live test embeds real questions with config.embed_model
and skips itself when Ollama can't be reached.

Run: venv/bin/python -m unittest discover tests
"""

import math
import unittest
from unittest import mock

import ollama_client
import tool_router
from config import config
from tools import TOOL_GROUPS, TOOLS, TOOLS_BY_NAME

def names(tools) -> list[str] | None:
    return None if tools is None else [tool.__name__ for tool in tools]

def group_tools(*groups: str) -> list[str]:
    """The tools sent when the router picks these groups: theirs and any tool in no group."""
    grouped = {name for group in TOOL_GROUPS.values() for name in group["tools"]}
    return [tool.__name__ for tool in TOOLS
            if tool.__name__ not in grouped or any(tool.__name__ in TOOL_GROUPS[g]["tools"] for g in groups)]

def ask(*questions: str) -> tuple[str, str | None]:
    """The arguments of select_tools for the last of these questions."""
    return questions[-1], questions[-2] if len(questions) > 1 else None

class FakeEmbeddings:
    """Embeds every example as a unit vector on its group's axis (None = no-tools examples), and
    each question in `scores` so that its cosine with each axis is the score given. An example in
    two groups gets an axis of its own, so neither group's score leaks into the other."""

    def __init__(self, scores: dict[str, dict]):
        self.scores = scores
        self.example_axis = {text: None for text in tool_router.NO_TOOL_EXAMPLES}
        shared = []
        for name, group in TOOL_GROUPS.items():
            for text in group["examples"]:
                if text in self.example_axis:
                    self.example_axis[text] = text
                    shared.append(text)
                else:
                    self.example_axis[text] = name
        self.axes = [None, *TOOL_GROUPS, *shared, "rest"]

    def vector(self, text: str) -> list[float]:
        if text in self.scores:
            wanted = self.scores[text]
            vector = [wanted.get(axis, 0.0) for axis in self.axes]
            vector[-1] = math.sqrt(1 - sum(v * v for v in vector))
            return vector
        return [1.0 if axis == self.example_axis[text] else 0.0 for axis in self.axes]

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]

class SelectToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)
        for patcher in [mock.patch.object(config, "tool_routing", True),
                        mock.patch.object(tool_router, "allowed_tool_names", return_value=None)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def embed(self, scores: dict[str, dict]):
        patcher = mock.patch.object(tool_router, "_embed", FakeEmbeddings(scores))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_confident_group(self):
        self.embed({"Is Vitality playing?": {"cs": 0.8, "anime": 0.3, None: 0.2}})
        selection = await tool_router.select_tools(*ask("Is Vitality playing?"))
        self.assertEqual(selection.groups, ["cs"])
        self.assertEqual(names(selection.tools), group_tools("cs"))

    async def test_groups_within_margin(self):
        self.embed({"q": {"calendar": 0.7, "anime": 0.55, "cs": 0.45}})
        selection = await tool_router.select_tools(*ask("q"))
        self.assertEqual(selection.groups, ["calendar", "anime"])
        self.assertEqual(names(selection.tools), group_tools("anime", "calendar"))

    async def test_group_outside_margin_left_out(self):
        self.embed({"q": {"anime": 0.8, "cs": 0.5}})
        selection = await tool_router.select_tools(*ask("q"))
        self.assertEqual(selection.groups, ["anime"])

    async def test_small_talk_gets_no_tools(self):
        self.embed({"Is NAVI on?": {"cs": 0.9}, "thanks!": {None: 0.7, "cs": 0.3}})
        selection = await tool_router.select_tools(*ask("Is NAVI on?", "thanks!"))
        self.assertEqual(selection.groups, [])
        self.assertEqual(names(selection.tools) or [], group_tools())

    async def test_follow_up_borrows_previous_topic(self):
        self.embed({
            "When is NAVI's next game?": {"cs": 0.8},
            "and the week after?": {"calendar": 0.3, "anime": 0.25, None: 0.1},
        })
        selection = await tool_router.select_tools(*ask("When is NAVI's next game?", "and the week after?"))
        # cs: 0.8 * PREVIOUS_WEIGHT = 0.64, confident
        self.assertEqual(selection.groups, ["cs"])

    async def test_short_follow_up_borrows_even_when_confident(self):
        self.embed({
            "Which anime air today?": {"anime": 0.8},
            "and tomorrow?": {"calendar": 0.5, None: 0.1},
        })
        selection = await tool_router.select_tools(*ask("Which anime air today?", "and tomorrow?"))
        # anime: 0.8 * PREVIOUS_WEIGHT = 0.64; calendar 0.5 is within MARGIN
        self.assertEqual(selection.groups, ["anime", "calendar"])

    async def test_confident_question_keeps_its_own_topic(self):
        self.embed({
            "Which anime air today?": {"anime": 0.8},
            "Do I have plans tomorrow?": {"calendar": 0.5, None: 0.1},
        })
        selection = await tool_router.select_tools(*ask("Which anime air today?", "Do I have plans tomorrow?"))
        self.assertEqual(selection.groups, ["calendar"])

    async def test_unsure_question_gets_top_groups(self):
        self.embed({"Who is donk?": {"anime": 0.35, "cs": 0.3, "web": 0.25, None: 0.1}})
        selection = await tool_router.select_tools(*ask("Who is donk?"))
        self.assertEqual(selection.groups, ["anime", "cs"])

    async def test_nothing_close_gets_no_tools(self):
        self.embed({"q": {"anime": 0.1, None: 0.05}})
        selection = await tool_router.select_tools(*ask("q"))
        self.assertEqual(selection.groups, [])
        self.assertEqual(names(selection.tools) or [], group_tools())

    async def test_ungrouped_tools_always_sent(self):
        self.embed({"q": {"cs": 0.8}})
        groups = {name: group for name, group in TOOL_GROUPS.items() if name != "web"}
        with mock.patch.dict(TOOL_GROUPS, groups, clear=True):
            selection = await tool_router.select_tools(*ask("q"))
        self.assertEqual(selection.groups, ["cs"])
        self.assertEqual(names(selection.tools), [
            name for name in names(TOOLS) if name in group_tools("cs") or name in ["search_web", "fetch_web_page"]])

    async def test_preset_in_one_group_skips_routing(self):
        embed = mock.AsyncMock()
        with mock.patch.object(tool_router, "_embed", embed), \
             mock.patch.object(tool_router, "allowed_tool_names", return_value=group_tools("cs")):
            selection = await tool_router.select_tools(*ask("hello"))
        embed.assert_not_called()
        self.assertIsNone(selection.groups)
        self.assertEqual(names(selection.tools), group_tools("cs"))

    async def test_preset_narrows_routing(self):
        self.embed({"q": {"anime": 0.9, "cs": 0.3, "calendar": 0.25}})
        allowed = ["get_cs_team", "get_calendar_events"]
        with mock.patch.object(tool_router, "allowed_tool_names", return_value=allowed):
            selection = await tool_router.select_tools(*ask("q"))
        # anime isn't allowed, so the best of cs and calendar wins, unsure
        self.assertEqual(selection.groups, ["cs", "calendar"])
        self.assertEqual(names(selection.tools), allowed)

    async def test_preset_without_tools(self):
        with mock.patch.object(tool_router, "allowed_tool_names", return_value=[]):
            selection = await tool_router.select_tools(*ask("hello"))
        self.assertIsNone(selection.tools)
        self.assertIsNone(selection.groups)

    async def test_routing_off_sends_every_allowed_tool(self):
        with mock.patch.object(config, "tool_routing", False):
            selection = await tool_router.select_tools(*ask("hello"))
        self.assertEqual(names(selection.tools), names(TOOLS))
        self.assertIsNone(selection.groups)

    async def test_router_error_sends_every_allowed_tool(self):
        with mock.patch.object(tool_router, "_embed", mock.AsyncMock(side_effect=ConnectionError("down"))):
            selection = await tool_router.select_tools(*ask("hello"))
        self.assertEqual(names(selection.tools), names(TOOLS))
        self.assertIsNone(selection.groups)

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
]

class LiveRoutingTest(unittest.IsolatedAsyncioTestCase):
    """Real questions through config.embed_model. Rerun after changing the embedding model,
    the group examples or the thresholds."""

    async def asyncSetUp(self):
        # Clients are bound to the event loop they were made on; each test gets a new one
        ollama_client._clients.clear()
        tool_router._examples.clear()
        self.addCleanup(ollama_client._clients.clear)
        self.addCleanup(tool_router._examples.clear)
        try:
            await tool_router._embed(["ping"])
        except Exception as e:
            self.skipTest(f"embedding model {config.embed_model} unreachable: {e}")

    async def scores(self, question: str) -> str:
        examples = await tool_router._example_vectors()
        vector = (await tool_router._embed([question]))[0]
        scores = tool_router._scores(vector, examples)
        return ", ".join(f"{name or 'no tools'} {score:.2f}"
                         for name, score in sorted(scores.items(), key=lambda s: -s[1]))

    async def test_questions_reach_their_groups(self):
        with mock.patch.object(config, "tool_routing", True), \
             mock.patch.object(tool_router, "allowed_tool_names", return_value=None):
            for questions, expected in LIVE_CASES:
                with self.subTest(questions[-1]):
                    groups = (await tool_router.select_tools(*ask(*questions))).groups
                    detail = f"chose {groups}; scores: {await self.scores(questions[-1])}"
                    if expected:
                        self.assertLessEqual(set(expected), set(groups), detail)
                        self.assertLessEqual(len(groups), max(len(expected), MAX_GROUPS), detail)
                    else:
                        self.assertEqual(groups, [], detail)

if __name__ == "__main__":
    unittest.main()
