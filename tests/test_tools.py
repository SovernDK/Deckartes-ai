"""Tests for the tool registry (tools.tool) and the presets that name tools (prompts.json)."""

import unittest
from unittest import mock

from agent import prompts
from core import prompt_file
from core.config import config
from frozen_clock import freeze
from tools import tools
from tools.tool_groups import GROUPS


class RegistryTest(unittest.TestCase):
    def test_every_group_has_tools_and_each_tool_is_in_one_group_at_most(self):
        self.assertEqual(GROUPS.groups_of(tools.TOOLS_BY_NAME), GROUPS.names)
        for name in tools.TOOLS_BY_NAME:
            self.assertLessEqual(len(GROUPS.groups_of([name])), 1, name)

    def test_lookup_by_name(self):
        self.assertIs(tools.TOOLS_BY_NAME["get_cs_team"], tools.get_cs_team)

    def test_decorator_returns_the_function(self):
        # tool_schema builds the tool definition from the function's own name, signature and docstring
        self.assertEqual(tools.get_cs_team.__name__, "get_cs_team")
        self.assertIn("Args:", tools.get_cs_team.__doc__ or "")

    def test_registering_in_an_unknown_group_raises(self):
        async def new_tool() -> str:
            return ""

        with self.assertRaisesRegex(ValueError, "unknown tool group 'nope'"):
            tools.tool("nope")(new_tool)


class PresetTest(unittest.TestCase):
    def test_presets_name_real_tools(self):
        for preset in prompts.load_prompts():
            if isinstance(preset.get("tools"), list):
                with self.subTest(preset["name"]):
                    self.assertLessEqual(set(preset["tools"]), set(tools.TOOLS_BY_NAME))

    def test_unknown_tool_in_a_preset_raises(self):
        preset = {"name": "Typo", "prompt": "...", "tools": ["get_cs_team", "get_cs_tem"]}
        with (
            mock.patch.object(prompts, "load_prompts", return_value=[preset]),
            mock.patch.object(config, "system_prompt", "Typo"),
            self.assertRaisesRegex(ValueError, "^preset 'Typo' names unknown tool 'get_cs_tem'$"),
        ):
            prompts.allowed_tool_names()

    def test_date_line_is_local_time(self):
        freeze(self, "2026-11-11 12:00")
        self.assertEqual(prompts.current_date_line(), "Current date: Wednesday 2026-11-11 (Europe/Warsaw).")
        self.assertEqual(prompts.current_time_line(), "Current local time: 12:00.")

    def test_presets_with_tools_are_told_to_use_what_they_know(self):
        presets = [{"name": "A", "prompt": "Help.", "tools": "all"}, {"name": "B", "prompt": "Chat.", "tools": []}]
        with mock.patch.object(prompts, "load_prompts", return_value=presets):
            with mock.patch.object(config, "system_prompt", "A"):
                self.assertTrue(prompts.system_message().endswith(f"Help. {prompt_file.task('tool_values_rule')}"))
            with mock.patch.object(config, "system_prompt", "B"):
                self.assertTrue(prompts.system_message().endswith("Chat."))

    def test_tools_for_picks_named_tools_and_none_means_all(self):
        self.assertEqual(tools.tools_for(["get_cs_team"]), [tools.get_cs_team])
        self.assertIs(tools.tools_for(None), tools.TOOLS)


if __name__ == "__main__":
    unittest.main()
