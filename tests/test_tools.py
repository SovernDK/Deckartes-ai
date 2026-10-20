"""Tests for the tool registry (tools.tool) and the presets that name tools (prompts.json)."""

import unittest
from unittest import mock

import prompts
import tools
from config import config

class RegistryTest(unittest.TestCase):
    def test_every_tool_is_in_its_group(self):
        grouped = [name for group in tools.TOOL_GROUPS.values() for name in group["tools"]]
        self.assertCountEqual(grouped, [tool.__name__ for tool in tools.TOOLS])
        for name, group in tools.TOOL_GROUPS.items():
            self.assertTrue(group["tools"], f"group {name} has no tools")

    def test_lookup_by_name(self):
        self.assertIs(tools.TOOLS_BY_NAME["get_cs_team"], tools.get_cs_team)

    def test_decorator_returns_the_function(self):
        # Ollama builds the tool definition from the function's own name, signature and docstring
        self.assertEqual(tools.get_cs_team.__name__, "get_cs_team")
        self.assertIn("Args:", tools.get_cs_team.__doc__)

    def test_unknown_group(self):
        with self.assertRaisesRegex(ValueError, "unknown tool group 'nope'"):
            tools.tool("nope")

class PresetTest(unittest.TestCase):
    def test_presets_name_real_tools(self):
        for preset in prompts.load_prompts():
            if isinstance(preset.get("tools"), list):
                with self.subTest(preset["name"]):
                    self.assertLessEqual(set(preset["tools"]), set(tools.TOOLS_BY_NAME))

    def test_unknown_tool_in_preset(self):
        preset = {"name": "Typo", "prompt": "...", "tools": ["get_cs_team", "get_cs_tem"]}
        with mock.patch.object(prompts, "load_prompts", return_value=[preset]), \
             mock.patch.object(config, "system_prompt", "Typo"):
            with self.assertRaisesRegex(ValueError, "^preset 'Typo' names unknown tool 'get_cs_tem'$"):
                prompts.allowed_tool_names()

    def test_tools_for(self):
        self.assertEqual(tools.tools_for(["get_cs_team"]), [tools.get_cs_team])
        self.assertIs(tools.tools_for(None), tools.TOOLS)

if __name__ == "__main__":
    unittest.main()
