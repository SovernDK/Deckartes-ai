"""The definitions the model sees, built from tool functions."""

import unittest

from tool_schema import tool_definition


async def everything(name: str, count: int, ratio: float, flag: bool, items: list[str],
                     maybe: str | None = None, defaulted: int = 3) -> str:
    """Do everything.

    Second line of the description.

    Args:
        name: The name
        count (integer): How many,
            continued here
        ratio: A ratio
        flag: A flag
        items: Some items
        maybe: Optional text
        defaulted: Has a default

    Returns:
        Text: never part of the definition
    """
    return name

async def no_arguments() -> str:
    """Nothing needed."""
    return ""

class ToolDefinitionTest(unittest.TestCase):
    def test_description_types_and_required(self):
        self.assertEqual(tool_definition(everything), {"type": "function", "function": {
            "name": "everything",
            "description": "Do everything.\n\nSecond line of the description.",
            "parameters": {
                "type": "object",
                "required": ["name", "count", "ratio", "flag", "items", "defaulted"],
                "properties": {
                    "name": {"description": "The name", "type": "string"},
                    "count": {"description": "How many, continued here", "type": "integer"},
                    "ratio": {"description": "A ratio", "type": "number"},
                    "flag": {"description": "A flag", "type": "boolean"},
                    "items": {"description": "Some items", "type": "array"},
                    "maybe": {"description": "Optional text", "type": "string"},
                    "defaulted": {"description": "Has a default", "type": "integer"},
                },
            },
        }})

    def test_no_arguments(self):
        self.assertEqual(tool_definition(no_arguments)["function"]["parameters"],
                         {"type": "object", "required": [], "properties": {}})

if __name__ == "__main__":
    unittest.main()
