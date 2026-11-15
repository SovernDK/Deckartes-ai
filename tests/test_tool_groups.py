"""Tests for ToolGroups: which tools go with the groups the router chooses."""

import unittest

from tools.tool_groups import ToolGroups


async def ask_server() -> str: ...
async def ask_web() -> str: ...
async def search_saved() -> str: ...
async def read_page() -> str: ...


class ToolGroupsTest(unittest.TestCase):
    def setUp(self):
        self.groups = ToolGroups({"server": ["Is the server up?"], "web": ["Search the web"], "empty": []})
        self.groups.add("server", "ask_server")
        self.groups.add("web", "ask_web")
        self.groups.add("web", "read_page")
        self.tools = [ask_server, ask_web, search_saved, read_page]

    def names(self, tools) -> list[str]:
        return [tool.__name__ for tool in tools]

    def test_the_chosen_groups_tools_and_tools_in_no_group_are_sent_in_their_order(self):
        self.assertEqual(
            self.names(self.groups.sent_with(["web"], self.tools)), ["ask_web", "search_saved", "read_page"]
        )

    def test_a_tool_in_no_group_is_sent_even_when_no_group_is_chosen(self):
        self.assertEqual(self.names(self.groups.sent_with([], self.tools)), ["search_saved"])
        self.assertTrue(self.groups.is_sent_with("search_saved", []))
        self.assertFalse(self.groups.is_sent_with("ask_server", ["web"]))

    def test_groups_of_tools_follow_group_order_and_skip_tools_in_no_group(self):
        self.assertEqual(self.groups.groups_of(["read_page", "search_saved", "ask_server"]), ["server", "web"])

    def test_names_and_examples_come_from_the_definition(self):
        self.assertEqual(self.groups.names, ["server", "web", "empty"])
        self.assertEqual(self.groups.examples("server"), ["Is the server up?"])

    def test_adding_to_an_unknown_group_raises(self):
        with self.assertRaisesRegex(ValueError, "unknown tool group 'nope'"):
            self.groups.add("nope", "ask_server")


if __name__ == "__main__":
    unittest.main()
