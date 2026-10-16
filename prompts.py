"""System prompt presets from prompts.json.

Each preset has a name, a description, the system prompt text and the tools it may use:
"all", a list of tool names from tools.TOOLS, or [] for none. Every system message starts
with the current date and time (models don't know it otherwise), so presets needn't mention it.
The file is read on every use, so edits apply without a restart.
"""

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from config import config

PROMPTS_FILE = Path(__file__).with_name("prompts.json")

def load_prompts() -> list[dict]:
    return json.loads(PROMPTS_FILE.read_text())["prompts"]

def current_prompt() -> dict | None:
    """The preset named config.system_prompt, or None if there is no such preset."""
    for prompt in load_prompts():
        if prompt["name"] == config.system_prompt:
            return prompt
    return None

def current_date_line() -> str:
    now = dt.datetime.now(ZoneInfo(config.timezone))
    return f"Current date: {now:%A %Y-%m-%d}, local time {now:%H:%M} ({config.timezone})."

def system_message() -> str:
    """The current date, then the preset's prompt; just the date if no preset matches."""
    prompt = current_prompt()
    if prompt is None:
        return current_date_line()
    return f"{current_date_line()}\n\n{prompt['prompt']}"

def allowed_tool_names() -> list[str] | None:
    """Tool names the current preset allows; None means all tools."""
    prompt = current_prompt()
    if prompt is None or prompt.get("tools", "all") == "all":
        return None
    return list(prompt["tools"])
