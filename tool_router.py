"""Picks which tool groups (tools.TOOL_GROUPS) to send with a question.

Tool definitions are most of the prompt (about 2,700 tokens for every tool, against 90 for
"hi" without them), so before each question a small embedding model (config.embed_model)
compares it with each group's example questions and with examples that need no tools:

- groups scoring at least CONFIDENT, and within MARGIN of the best, are sent;
- otherwise no tools when the no-tools examples match better or nothing scores MIN_SCORE;
- otherwise the UNSURE_GROUPS best groups, since unknown names (a player, a show) score low.

When no group is confident, the previous question counts at PREVIOUS_WEIGHT, so a follow-up
like "and next week?" keeps its topic. Thresholds were tuned on all-minilm; another embedding model may need new ones.
"""

import asyncio
import math

from config import config
from tools import TOOL_GROUPS

CONFIDENT = 0.45
# "What anime airs tomorrow?" scores 0.95 for anime but also 0.46 for cs
MARGIN = 0.2
MIN_SCORE = 0.2
UNSURE_GROUPS = 2
PREVIOUS_WEIGHT = 0.8

# Questions the model answers from its own knowledge
NO_TOOL_EXAMPLES = [
    "hello", "thanks, that helps", "tell me a joke", "explain how recursion works", "write a poem",
    "what is 12 times 7", "translate this sentence to Spanish", "fix this python code",
    "what is the capital of France", "how are you?", "summarize what we talked about",
    "give me a recipe for pancakes", "explain quantum physics simply",
]

# Example embeddings per (host, model); computed once, on first use
_examples: dict[tuple[str, str], dict[str, list[list[float]]]] = {}

async def _embed(texts: list[str]) -> list[list[float]]:
    # The sync client in a thread: fetch_response runs this under a new event loop each time,
    # which the cached async client (bound to the first loop) can't serve
    from ollama_api import get_client
    response = await asyncio.to_thread(get_client().embed, model=config.embed_model, input=texts)
    return response.embeddings

async def _example_vectors() -> dict[str, list[list[float]]]:
    key = (config.host, config.embed_model)
    if key not in _examples:
        groups = {name: group["examples"] for name, group in TOOL_GROUPS.items()}
        groups[None] = NO_TOOL_EXAMPLES
        texts = [text for examples in groups.values() for text in examples]
        vectors = iter(await _embed(texts))
        _examples[key] = {name: [next(vectors) for _ in examples] for name, examples in groups.items()}
    return _examples[key]

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))

def _scores(vector: list[float], examples: dict) -> dict:
    """Best similarity to each group's examples (None = the no-tools examples)."""
    return {name: max(cosine(vector, e) for e in vectors) for name, vectors in examples.items()}

async def route(query: str, previous: str | None, groups: list[str]) -> list[str]:
    """Names of the groups (among groups) to send for query; [] means no tools.
    Raises if the embedding model can't be reached."""
    examples = {name: vectors for name, vectors in (await _example_vectors()).items()
                if name is None or name in groups}
    vectors = await _embed([query, previous] if previous else [query])
    scores = _scores(vectors[0], examples)
    no_tools = scores.pop(None)
    if not scores:
        return []
    best = max(scores.values())
    if best < CONFIDENT:
        # Small talk ("thanks!") needs no tools, whatever the previous topic was
        if no_tools > best:
            return []
        # A vague question (a follow-up) borrows the previous question's topic
        if previous:
            for name, score in _scores(vectors[1], examples).items():
                if name is not None:
                    scores[name] = max(scores[name], PREVIOUS_WEIGHT * score)
            best = max(scores.values())

    ranked = sorted(scores, key=scores.get, reverse=True)
    if best >= CONFIDENT:
        return [name for name in ranked if scores[name] >= max(CONFIDENT, best - MARGIN)]
    if best < MIN_SCORE:
        return []
    return ranked[:UNSURE_GROUPS]
