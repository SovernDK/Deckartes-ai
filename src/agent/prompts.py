"""System prompt presets from prompts.json.

Each preset has a name, a description, the system prompt text and the tools it may use:
"all", a list of tool names from tools.TOOLS, or [] for none. Every system message starts
with the current date (models don't know it otherwise), so presets needn't mention it.
The time of day goes just before each question instead (current_time_line): the start of the
prompt has to stay the same from minute to minute for the server's prompt cache to work.
The file is read on every use, so edits apply without a restart.
"""

import json

from core import clock
from core.config import PROJECT_ROOT, config
from tools.tools import TOOLS_BY_NAME

PROMPTS_FILE = PROJECT_ROOT / "prompts.json"

# Added to every preset that has tools: small models otherwise ask for values they already have
TOOL_VALUES_RULE = ("When a tool needs a value, take it from the conversation so far or leave out what is "
                    "optional, and call the tool instead of asking the user for it.")

def load_prompts() -> list[dict]:
    return json.loads(PROMPTS_FILE.read_text())["prompts"]

def current_prompt() -> dict | None:
    for prompt in load_prompts():
        if prompt["name"] == config.system_prompt:
            return prompt
    return None

def current_date_line() -> str:
    return f"Current date: {clock.now():%A %Y-%m-%d} ({config.timezone})."

def current_time_line() -> str:
    return f"Current local time: {clock.now():%H:%M}."

def system_message() -> str:
    prompt = current_prompt()
    if prompt is None:
        return current_date_line()
    rule = f" {TOOL_VALUES_RULE}" if prompt.get("tools", "all") else ""
    return f"{current_date_line()}\n\n{prompt['prompt']}{rule}"

def allowed_tool_names() -> list[str] | None:
    """Tool names the current preset allows; None means all tools.
    Raises ValueError for a name that isn't a tool, so a typo in prompts.json shows as an error."""
    prompt = current_prompt()
    if prompt is None or prompt.get("tools", "all") == "all":
        return None
    for name in prompt["tools"]:
        if name not in TOOLS_BY_NAME:
            raise ValueError(f"preset {prompt['name']!r} names unknown tool {name!r}")
    return list(prompt["tools"])
