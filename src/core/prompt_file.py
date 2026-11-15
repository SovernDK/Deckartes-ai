"""Every prompt the app sends a model, from prompts.json at the repo root.

"presets" are the system prompts the user picks from (agent/prompts.py). "tasks" are the fixed
prompts of single jobs, like summarising a conversation or commenting on a spending digest, with
{placeholders} filled in by task(). "decisions" are the questions the decision model answers by
picking one of their options, like a payee's spending category (decision()). The file is read on every use, so edits apply without a restart.
It imports nothing from tools, so tool backends can read their prompts too.
"""

import json

from core.config import PROJECT_ROOT

PROMPTS_FILE = PROJECT_ROOT / "prompts.json"


def _read() -> dict:
    return json.loads(PROMPTS_FILE.read_text())


def presets() -> list[dict]:
    return _read()["presets"]


def task(name: str, **values: str) -> str:
    """A task prompt with its {placeholders} filled in from values."""
    return _read()["tasks"][name].format(**values)


def decision(name: str) -> dict:
    """A decision model's question: its "question", its "options" (answer -> description) and any
    words the job writes the question's state with."""
    return _read()["decisions"][name]
