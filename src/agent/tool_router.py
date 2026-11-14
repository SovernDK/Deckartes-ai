"""Picks which tool groups (tool_groups.GROUPS) to send with a question.

Tool definitions are most of the prompt (about 2,700 tokens for every tool, against 90 for
"hi" without them), so before each question a small embedding model (config.embed_model)
compares it with each group's example questions and with examples that need no tools:

- groups scoring at least CONFIDENT, and within MARGIN of the best, are sent;
- otherwise no tools when the no-tools examples match better or nothing scores MIN_SCORE;
- otherwise the UNSURE_GROUPS best groups, since unknown names (a player, a show) score low.

When no group is confident, or the question is a short follow-up ("and tomorrow?", which can
still match calendar on its own), the previous question counts at PREVIOUS_WEIGHT, so it keeps its topic. Thresholds were checked on all-MiniLM-L6-v2; another embedding model may need new ones.
"""

from dataclasses import dataclass

import numpy as np

from agent.prompts import allowed_tool_names
from core.config import config
from model import mlx_client
from tools.tool_groups import GROUPS
from tools.tools import tools_for

CONFIDENT = 0.45
# "What anime airs tomorrow?" scores 0.95 for anime but also 0.46 for cs, and "Add Vitality's
# next match to my calendar" 0.70 for cs and calendar but also 0.54 for anime
MARGIN = 0.15
MIN_SCORE = 0.2
UNSURE_GROUPS = 2
PREVIOUS_WEIGHT = 0.8
# A question this short starting like this is a follow-up to the previous one
FOLLOW_UP_WORDS = 4
FOLLOW_UP_STARTS = ("and ", "what about ", "how about ")
# Replies to something the model asked: they say nothing of the topic and read like small talk
FOLLOW_UP_ANSWERS = {"yes", "yes please", "ok", "okay", "sure", "do it", "go ahead"}

NO_TOOL_EXAMPLES = [
    "hello", "thanks, that helps", "tell me a joke", "explain how recursion works", "write a poem",
    "what is 12 times 7", "translate this sentence to Spanish", "fix this python code",
    "what is the capital of France", "how are you?", "summarize what we talked about",
    "give me a recipe for pancakes", "explain quantum physics simply",
    "what is 20 percent of 150", "calculate 7% of 300", "how many minutes are in a day",
]

# Example embeddings per (host, model), one matrix of unit rows per group; computed once, on first use
_examples: dict[tuple[str, str], dict[str | None, np.ndarray]] = {}

async def _embed(texts: list[str]) -> list[list[float]]:
    return await mlx_client.embed(texts)

def _unit(vectors: list[list[float]]) -> np.ndarray:
    """The vectors as rows scaled to length 1, so a matrix product gives their cosines."""
    matrix = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if not norms.all():
        raise ValueError("The embedding model returned an all-zero vector")
    return matrix / norms

async def _example_vectors() -> dict[str | None, np.ndarray]:
    key = (config.host, config.embed_model)
    if key not in _examples:
        groups: dict[str | None, list[str]] = {name: GROUPS.examples(name) for name in GROUPS.names}
        groups[None] = NO_TOOL_EXAMPLES
        example_texts = [example for group_examples in groups.values() for example in group_examples]
        vectors = _unit(await _embed(example_texts))
        bounds = np.cumsum([0, *(len(group_examples) for group_examples in groups.values())])
        _examples[key] = {name: vectors[start:end] for name, start, end in zip(groups, bounds, bounds[1:])}
    return _examples[key]

def _scores(vector: np.ndarray, examples: dict) -> dict:
    """Best cosine similarity of a unit vector to each group's examples (None = the no-tools examples)."""
    return {name: float((matrix @ vector).max()) for name, matrix in examples.items()}

def _is_follow_up(query: str) -> bool:
    text = query.strip().lower()
    if len(text.split()) > FOLLOW_UP_WORDS:
        return False
    # A short message with a number ("18 to 20") is the missing detail of the question before it
    return text.startswith(FOLLOW_UP_STARTS) or text.rstrip("!. ") in FOLLOW_UP_ANSWERS \
        or any(character.isdigit() for character in text)

async def _route(query: str, previous: str | None, groups: list[str]) -> tuple[list[str], dict[str, float]]:
    """Names of the groups (among groups) to send for query, [] meaning no tools, and the scores
    they were chosen on: each group's, and "no_tools". Raises if the embedding model can't be reached."""
    examples = {name: vectors for name, vectors in (await _example_vectors()).items()
                if name is None or name in groups}
    vectors = _unit(await _embed([query, previous] if previous else [query]))
    scores = _scores(vectors[0], examples)
    no_tools = scores.pop(None)
    def chosen(group_names: list[str]) -> tuple[list[str], dict[str, float]]:
        return group_names, {**scores, "no_tools": no_tools}
    if not scores:
        return chosen([])
    best = max(scores.values())
    # Small talk ("thanks!") needs no tools, whatever the previous topic was
    if best < CONFIDENT and no_tools > best and not (previous and _is_follow_up(query)):
        return chosen([])
    # A vague question or a short follow-up borrows the previous question's topic
    if previous and (best < CONFIDENT or _is_follow_up(query)):
        for name, previous_score in _scores(vectors[1], examples).items():
            if name is not None:
                scores[name] = max(scores[name], PREVIOUS_WEIGHT * previous_score)
        best = max(scores.values())

    ranked = sorted(scores, key=lambda name: scores[name], reverse=True)
    if best >= CONFIDENT:
        return chosen([name for name in ranked if scores[name] >= max(CONFIDENT, best - MARGIN)])
    if best < MIN_SCORE:
        return chosen([])
    return chosen(ranked[:UNSURE_GROUPS])

@dataclass
class Selection:
    # Tools to send with the question; None for none
    tools: list | None
    # Groups the router chose; None when it didn't run
    groups: list[str] | None
    # What it chose them on: each group's score and "no_tools"; None when it didn't run
    scores: dict[str, float] | None = None

async def select_tools(question: str, previous: str | None) -> Selection:
    """Tools for a question (previous is the one before it, if any): those the current prompt preset allows, narrowed
    to the groups the question needs. Routing is skipped when the preset's tools are all in one
    group, and falls back to every allowed tool if the router fails. Tools in no group are always sent."""
    tools = tools_for(allowed_tool_names())
    if not tools:
        return Selection(None, None)

    groups = GROUPS.groups_of(function.__name__ for function in tools)
    if not config.tool_routing or len(groups) < 2:
        return Selection(tools, None)
    try:
        chosen, scores = await _route(question, previous, groups)
    except Exception:
        return Selection(tools, None)
    routed = GROUPS.sent_with(chosen, tools)
    return Selection(routed or None, chosen, scores)
