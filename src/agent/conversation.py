"""Conversation with the AI model: history and turns, independent of the UI.

Conversation.stream_reply runs a whole turn: it builds what the model is sent, picks the tools,
runs any tool calls the model makes and yields the turn as events. It never raises: any failure
is a Failed event, always last.

A turn is added to the history only when the model finishes it without error. A turn
that fails or is interrupted (its stream closed or task cancelled) leaves the history as it was.
Tokens are counted for every turn, including failed ones, since the model still used them.

When the last turn's context reached config.compact_at of the context window, the next turn
first replaces the whole history with the model's summary of it (one system message), so a long
conversation keeps fitting. A failed summary fails that turn and leaves the history as it was.

The time of day is a system message just before each question. It is saved in the history with
the turn, so the earlier turns of the next prompt match the cached ones exactly.
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field

from agent.prompts import current_time_line, system_message
from agent.tool_router import select_tools
from core.config import config
from model import mlx_client
from tools.tools import run_tool


# What a turn yields, in order
@dataclass
class Text:
    """Part of the reply."""
    text: str

@dataclass
class ToolStarted:
    """A tool the model called is about to run."""
    name: str

@dataclass
class ToolsDone:
    """The tools of one round have run; the model is answering again."""

@dataclass
class Compacting:
    """The history is being summarised before the question is answered."""

@dataclass
class Compacted:
    """The history is now a summary; the question is being answered."""

@dataclass
class Failed:
    """The turn failed; always the last event. The reply so far isn't saved."""
    message: str

Event = Text | ToolStarted | ToolsDone | Compacting | Compacted | Failed

# Stop a model that keeps calling tools instead of answering
MAX_TOOL_ROUNDS = 10

@dataclass
class Usage:
    """Tokens the server reports for chat calls: prompt tokens (in) and generated (out).
    A reply with tool calls makes several calls, each re-sending the conversation so far."""
    input: int = 0
    output: int = 0
    # Nanoseconds spent generating the output tokens, without model loading or prompt evaluation
    output_ns: int = 0

    def count(self, tokens: mlx_client.TokenCount | None) -> None:
        """Add a finished chat call's token counts."""
        if tokens is not None:
            self.input += tokens.prompt
            self.output += tokens.completion
            self.output_ns += tokens.generation_ns

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.output_ns += other.output_ns

    @property
    def tokens_per_second(self) -> float | None:
        """Generation speed; None when the server reported no generation time."""
        return self.output / self.output_ns * 1e9 if self.output_ns else None

    @property
    def total(self) -> int:
        return self.input + self.output

@dataclass
class TurnInfo:
    """What one turn used and what it was sent with, for the usage line under its reply."""
    usage: Usage = field(default_factory=Usage)
    # Tool groups the router sent with the question; None when it didn't run
    tool_groups: list[str] | None = None

    def summary(self) -> str | None:
        """The usage line; None when the turn never reached the model."""
        usage = self.usage
        if not usage.total:
            return None
        text = f"{usage.input:,} in · {usage.output:,} out · {usage.total:,} {'token' if usage.total == 1 else 'tokens'}"
        if usage.tokens_per_second is not None:
            text += f" · {usage.tokens_per_second:.1f} tok/s"
        if self.tool_groups is not None:
            text += f" · tools: {', '.join(self.tool_groups) or 'none'}"
        return text

SUMMARY_PROMPT = (
    "Summarise the conversation below so it can replace it as your only memory of it. "
    "Keep what the user wants, facts and numbers they gave, decisions made, results found "
    "(including tool results) and anything still open. Drop greetings and wording. "
    "Write plain text, as short as the content allows."
)

def _add_tool_results(messages, content, tool_calls, results) -> None:
    """Append the model's tool calls and their results so the next request sees them."""
    messages.append({"role": "assistant", "content": content,
                     "tool_calls": [call.as_message() for call in tool_calls]})
    for call, result in zip(tool_calls, results):
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

async def _run_call(call: mlx_client.ToolCall, runner) -> str:
    if call.arguments is None:
        return f"Tool {call.name} failed: its arguments were not valid JSON"
    return await runner(call.name, call.arguments)

class Conversation:
    def __init__(self, tool_runner=None, tool_selector=None) -> None:
        # What runs tools and picks them; a validation run replaces both (validation.py)
        self._tool_runner = tool_runner
        self._tool_selector = tool_selector
        # Finished turns as chat messages: for each, its time line, the question and the reply
        self.history: list[dict] = []
        self.last_turn = TurnInfo()
        # Tokens of the whole conversation
        self.total_usage = Usage()
        # Size of the context of the last finished turn: its final prompt plus reply, in tokens
        self.context_tokens = 0

    def clear(self) -> None:
        self.history.clear()
        self.context_tokens = 0
        self.last_turn = TurnInfo()
        self.total_usage = Usage()

    def _messages_for(self, query: str, time_line: str) -> list[dict]:
        """What gets sent to the model for a new question: the current system prompt
        (read fresh, so a changed preset applies from the next turn), the history, the time and
        the question."""
        system = system_message()
        messages = [{"role": "system", "content": system}] if system else []
        return [*messages, *self.history, {"role": "system", "content": time_line},
                {"role": "user", "content": query}]

    def _remember(self, query: str, reply: str, time_line: str) -> None:
        if reply:
            self.history.append({"role": "system", "content": time_line})
            self.history.append({"role": "user", "content": query})
            self.history.append({"role": "assistant", "content": reply})

    def needs_compacting(self) -> bool:
        """Whether the last turn's context reached config.compact_at of the window."""
        return bool(self.history and config.compact_at
                    and self.context_tokens >= config.compact_at * config.context_length)

    async def _compact(self, usage: Usage) -> None:
        """Replace the history with the model's summary of it."""
        transcript = "\n\n".join(f"{m['role']}: {m['content']}" for m in self.history)
        response = await mlx_client.complete([{"role": "system", "content": SUMMARY_PROMPT},
                                              {"role": "user", "content": transcript}])
        usage.count(response.usage)
        summary = response.content.strip()
        if not summary:
            raise ValueError("the model returned an empty summary")
        self.history = [{"role": "system", "content": f"Summary of the conversation so far:\n{summary}"}]
        self.context_tokens = 0

    async def stream_reply(self, query: str) -> AsyncIterator[Event]:
        """Run one turn, yielding its events. A Failed comes last and the turn isn't saved."""
        self.last_turn = turn = TurnInfo()
        if self.needs_compacting():
            yield Compacting()
            try:
                await self._compact(turn.usage)
            except Exception as e:
                self.total_usage.add(turn.usage)
                yield Failed(f"could not compact the conversation: {e}")
                return
            yield Compacted()
        questions = [m["content"] for m in self.history if m["role"] == "user"]
        previous = questions[-1] if questions else None
        time_line = current_time_line()
        try:
            messages = self._messages_for(query, time_line)
        except Exception as e:
            # e.g. a broken prompts.json
            yield Failed(f"could not load the system prompt: {e}")
            return
        reply = ""
        try:
            selection = await (self._tool_selector or select_tools)(query, previous)
            tools = selection.tools
            turn.tool_groups = selection.groups
            for _ in range(MAX_TOOL_ROUNDS):
                content = ""
                tool_calls = []
                context = 0
                # aclosing: a closed turn stops the model's request at once, not when Python gets to it
                async with aclosing(mlx_client.stream_chat(messages, tools)) as stream:
                    async for delta in stream:
                        if delta.content:
                            content += delta.content
                            reply += delta.content
                            yield Text(delta.content)
                        tool_calls.extend(delta.tool_calls)
                        if delta.usage is not None:
                            turn.usage.count(delta.usage)
                            context = delta.usage.prompt + delta.usage.completion

                if not tool_calls:
                    self._remember(query, reply, time_line)
                    if reply:
                        self.context_tokens = context
                    return
                if content:
                    reply += "\n\n"
                    yield Text("\n\n")
                results = []
                for call in tool_calls:
                    yield ToolStarted(call.name)
                    results.append(await _run_call(call, self._tool_runner or run_tool))
                yield ToolsDone()
                _add_tool_results(messages, content, tool_calls, results)

            yield Failed(f"model still calling tools after {MAX_TOOL_ROUNDS} rounds")
        except Exception as e:
            yield Failed(str(e))
        finally:
            self.total_usage.add(turn.usage)
