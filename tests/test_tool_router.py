"""Tests for tool_router.select_tools.

The rule tests swap mlx_client.embed for fake vectors, so each question gets exactly the
similarity scores a test asks for. Real questions through the embedding model are in
test_live_routing.py.

Run: venv/bin/python -m unittest discover tests
"""

import math
import unittest
from unittest import mock

from agent import prompts, tool_router
from core.config import config
from model import mlx_client
from tools.tool_groups import GROUPS
from tools.tools import TOOLS


def names(tools) -> list[str] | None:
    return None if tools is None else [tool.__name__ for tool in tools]

def group_tools(*groups: str) -> list[str]:
    """The tools sent when the router picks these groups (the rule is tested in test_tool_groups.py)."""
    return names(GROUPS.sent_with(groups, TOOLS))

def ask(*questions: str) -> tuple[str, str | None]:
    """The arguments of select_tools for the last of these questions."""
    return questions[-1], questions[-2] if len(questions) > 1 else None

def use_preset(test: unittest.TestCase, tools: str | list[str]) -> None:
    """Make the current preset one allowing these tools ("all" or names) for the rest of the test."""
    preset = {"name": "Test", "prompt": "", "tools": tools}
    for target, name, value in [(prompts, "load_prompts", mock.Mock(return_value=[preset])),
                                (config, "system_prompt", "Test")]:
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)

class FakeEmbeddings:
    """Embeds every example as a unit vector on its group's axis (None = no-tools examples), and
    each question in `scores` so that its cosine with each axis is the score given. An example in
    two groups gets an axis of its own, so neither group's score leaks into the other."""

    def __init__(self, scores: dict[str, dict]):
        self.scores = scores
        self.example_axis = {text: None for text in tool_router.NO_TOOL_EXAMPLES}
        shared = []
        for name in GROUPS.names:
            for text in GROUPS.examples(name):
                if text in self.example_axis:
                    self.example_axis[text] = text
                    shared.append(text)
                else:
                    self.example_axis[text] = name
        self.axes = [None, *GROUPS.names, *shared, "rest"]

    def vector(self, text: str) -> list[float]:
        if text in self.scores:
            wanted = self.scores[text]
            vector = [wanted.get(axis, 0.0) for axis in self.axes]
            vector[-1] = math.sqrt(1 - sum(value * value for value in vector))
            return vector
        return [1.0 if axis == self.example_axis[text] else 0.0 for axis in self.axes]

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]

class SelectToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)
        patcher = mock.patch.object(config, "tool_routing", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        use_preset(self, "all")

    def embed(self, scores: dict[str, dict]):
        patcher = mock.patch.object(mlx_client, "embed", FakeEmbeddings(scores))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_clear_winner_is_the_only_group_sent(self):
        self.embed({"Is Vitality playing?": {"cs": 0.8, "anime": 0.3, None: 0.2}})
        selection = await tool_router.select_tools(*ask("Is Vitality playing?"))
        self.assertEqual(selection.groups, ["cs"])
        self.assertEqual(names(selection.tools), group_tools("cs"))

    async def test_selection_reports_each_group_score_and_the_no_tools_score(self):
        self.embed({"q": {"cs": 0.8, "anime": 0.3, None: 0.2}})
        selection = await tool_router.select_tools(*ask("q"))
        self.assertAlmostEqual(selection.scores["cs"], 0.8)
        self.assertAlmostEqual(selection.scores["anime"], 0.3)
        self.assertAlmostEqual(selection.scores["no_tools"], 0.2)

    async def test_reported_scores_include_what_a_follow_up_borrowed(self):
        self.embed({"When is NAVI's next game?": {"cs": 0.9}, "and then?": {"cs": 0.1, None: 0.05}})
        selection = await tool_router.select_tools(*ask("When is NAVI's next game?", "and then?"))
        self.assertAlmostEqual(selection.scores["cs"], 0.72)

    async def test_groups_within_the_margin_of_the_best_are_sent_too(self):
        self.embed({"q": {"calendar": 0.7, "anime": 0.55, "cs": 0.45}})
        selection = await tool_router.select_tools(*ask("q"))
        self.assertEqual(selection.groups, ["calendar", "anime"])
        self.assertEqual(names(selection.tools), group_tools("anime", "calendar"))

    async def test_group_outside_the_margin_is_left_out(self):
        self.embed({"q": {"anime": 0.8, "cs": 0.5}})
        selection = await tool_router.select_tools(*ask("q"))
        self.assertEqual(selection.groups, ["anime"])

    async def test_small_talk_gets_no_tools(self):
        self.embed({"Is NAVI on?": {"cs": 0.9}, "thanks!": {None: 0.7, "cs": 0.3}})
        selection = await tool_router.select_tools(*ask("Is NAVI on?", "thanks!"))
        self.assertEqual(selection.groups, [])
        self.assertEqual(names(selection.tools) or [], group_tools())

    async def test_short_answer_with_a_number_keeps_the_topic(self):
        self.embed({"Add this to my calendar": {"calendar": 0.9}, "18 to 20": {None: 0.5, "cs": 0.1}})
        selection = await tool_router.select_tools(*ask("Add this to my calendar", "18 to 20"))
        self.assertEqual(selection.groups, ["calendar"])

    async def test_yes_keeps_the_topic_but_thanks_does_not(self):
        scores = {"Add this to my calendar": {"calendar": 0.9}, "Yes!": {None: 0.5}, "thanks": {None: 0.5}}
        self.embed(scores)
        self.assertEqual((await tool_router.select_tools(*ask("Add this to my calendar", "Yes!"))).groups, ["calendar"])
        self.assertEqual((await tool_router.select_tools(*ask("Add this to my calendar", "thanks"))).groups, [])

    async def test_number_without_a_previous_question_is_small_talk(self):
        self.embed({"18 to 20": {None: 0.5, "cs": 0.1}})
        self.assertEqual((await tool_router.select_tools(*ask("18 to 20"))).groups, [])

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

    async def test_preset_in_one_group_skips_routing(self):
        embed = mock.AsyncMock()
        use_preset(self, group_tools("cs"))
        with mock.patch.object(mlx_client, "embed", embed):
            selection = await tool_router.select_tools(*ask("hello"))
        embed.assert_not_called()
        self.assertIsNone(selection.groups)
        self.assertEqual(names(selection.tools), group_tools("cs"))

    async def test_preset_narrows_routing(self):
        self.embed({"q": {"anime": 0.9, "cs": 0.3, "calendar": 0.25}})
        allowed = ["get_cs_team", "get_calendar_events"]
        use_preset(self, allowed)
        selection = await tool_router.select_tools(*ask("q"))
        # anime isn't allowed, so the best of cs and calendar wins, unsure
        self.assertEqual(selection.groups, ["cs", "calendar"])
        self.assertEqual(names(selection.tools), allowed)

    async def test_preset_without_tools_sends_none(self):
        use_preset(self, [])
        selection = await tool_router.select_tools(*ask("hello"))
        self.assertIsNone(selection.tools)
        self.assertIsNone(selection.groups)

    async def test_routing_off_sends_every_allowed_tool(self):
        with mock.patch.object(config, "tool_routing", False):
            selection = await tool_router.select_tools(*ask("hello"))
        self.assertEqual(names(selection.tools), names(TOOLS))
        self.assertIsNone(selection.groups)

    async def test_router_error_sends_every_allowed_tool(self):
        with mock.patch.object(mlx_client, "embed", mock.AsyncMock(side_effect=ConnectionError("down"))):
            selection = await tool_router.select_tools(*ask("hello"))
        self.assertEqual(names(selection.tools), names(TOOLS))
        self.assertIsNone(selection.groups)

if __name__ == "__main__":
    unittest.main()
