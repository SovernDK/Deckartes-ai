"""Conversation with the AI model: history and turns, independent of the UI.

Conversation.stream_reply runs a whole turn: it builds what the model is sent, picks the tools,
runs any tool calls the model makes and yields the turn as events. It never raises: any failure
is a Failed event, always last.

A turn is added to the history only when the model finishes it without error. A turn
that fails or is interrupted (its stream closed or task cancelled) leaves the history as it was.
Tokens are counted for every turn, including failed ones, since the model still used them.

A loaded file (loaded_file.py) goes into every turn: a short file whole, at the end of the system message,
where it stays the same each turn so Ollama can reuse its cache; a long one as excerpts picked
for the question, just before it. Neither is saved in the history.
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field

from config import config
from ollama_client import get_async_client
from prompts import system_message
from loaded_file import FileContext, LoadedFile
from tool_router import select_tools
from tools import run_tool

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
class Failed:
    """The turn failed; always the last event. The reply so far isn't saved."""
    message: str

Event = Text | ToolStarted | ToolsDone | Failed

# Stop a model that keeps calling tools instead of answering
MAX_TOOL_ROUNDS = 10

@dataclass
class Usage:
    """Tokens Ollama reports for chat calls: prompt tokens evaluated (in) and generated (out).
    A reply with tool calls makes several calls, each re-sending the conversation so far."""
    input: int = 0
    output: int = 0
    # Nanoseconds spent generating the output tokens, without model loading or prompt evaluation
    output_ns: int = 0

    def count(self, response) -> None:
        """Add a finished chat response's token counts."""
        self.input += response.prompt_eval_count or 0
        self.output += response.eval_count or 0
        self.output_ns += response.eval_duration or 0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.output_ns += other.output_ns

    @property
    def tokens_per_second(self) -> float | None:
        """Generation speed; None when Ollama reported no generation time."""
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
    # What was sent from the loaded file: "whole" or "5 excerpts"; None without a file
    file_context: str | None = None

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
        if self.file_context:
            text += f" · file: {self.file_context}"
        return text

_tool_support = {}

async def _supports_tools() -> bool:
    """Whether the current model accepts tools; cached per host and model."""
    key = (config.host, config.model)
    if key not in _tool_support:
        info = await get_async_client().show(config.model)
        _tool_support[key] = "tools" in (info.capabilities or [])
    return _tool_support[key]

def _model_options() -> dict:
    """Ollama options from config: context window and, unless left to the model, temperature."""
    options = {"num_ctx": config.context_length}
    if config.temperature is not None:
        options["temperature"] = config.temperature
    return options

def _add_tool_results(messages, content, tool_calls, results) -> None:
    """Append the model's tool calls and their results so the next request sees them."""
    messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
    for call, result in zip(tool_calls, results):
        messages.append({"role": "tool", "tool_name": call.function.name, "content": result})

class Conversation:
    def __init__(self) -> None:
        # Finished turns in the Ollama messages format
        self.history: list[dict] = []
        self.last_turn = TurnInfo()
        # Tokens of the whole conversation
        self.total_usage = Usage()
        self.file: LoadedFile | None = None

    def clear(self) -> None:
        self.history.clear()
        self.last_turn = TurnInfo()
        self.total_usage = Usage()
        self.file = None

    def _messages_for(self, query: str, file: FileContext | None) -> list[dict]:
        """What gets sent to the model for a new question: the current system prompt
        (read fresh, so a changed preset applies from the next turn), the history and the question,
        with the loaded file: whole in the system message, or excerpts just before the question."""
        system = system_message()
        if file is not None and file.system:
            system = f"{system}\n\n{file.system}"
        messages = [{"role": "system", "content": system}] if system else []
        before = [{"role": "system", "content": file.excerpts}] if file is not None and file.excerpts else []
        return [*messages, *self.history, *before, {"role": "user", "content": query}]

    def _remember(self, query: str, reply: str) -> None:
        if reply:
            self.history.append({"role": "user", "content": query})
            self.history.append({"role": "assistant", "content": reply})

    async def stream_reply(self, query: str) -> AsyncIterator[Event]:
        """Run one turn, yielding its events. A Failed comes last and the turn isn't saved."""
        self.last_turn = turn = TurnInfo()
        questions = [m["content"] for m in self.history if m["role"] == "user"]
        previous = questions[-1] if questions else None
        try:
            file = await self.file.context_for(query, previous) if self.file is not None else None
        except Exception as e:
            yield Failed(f"could not search the loaded file: {e}")
            return
        try:
            messages = self._messages_for(query, file)
        except Exception as e:
            # e.g. a broken prompts.json
            yield Failed(f"could not load the system prompt: {e}")
            return
        turn.file_context = file.label if file is not None else None
        reply = ""
        try:
            tools = None
            if await _supports_tools():
                selection = await select_tools(query, previous)
                tools = selection.tools
                turn.tool_groups = selection.groups
            for _ in range(MAX_TOOL_ROUNDS):
                stream = await get_async_client().chat(
                    model=config.model,
                    messages=messages,
                    tools=tools,
                    options=_model_options(),
                    stream=True,
                )
                content = ""
                tool_calls = []
                # aclosing: a closed turn stops Ollama's request at once, not when Python gets to it
                async with aclosing(stream):
                    async for chunk in stream:
                        if chunk.message.content:
                            content += chunk.message.content
                            reply += chunk.message.content
                            yield Text(chunk.message.content)
                        if chunk.message.tool_calls:
                            tool_calls.extend(chunk.message.tool_calls)
                        if chunk.done:
                            turn.usage.count(chunk)

                if not tool_calls:
                    self._remember(query, reply)
                    return
                if content:
                    reply += "\n\n"
                    yield Text("\n\n")
                results = []
                for call in tool_calls:
                    yield ToolStarted(call.function.name)
                    results.append(await run_tool(call.function.name, call.function.arguments))
                yield ToolsDone()
                _add_tool_results(messages, content, tool_calls, results)

            yield Failed(f"model still calling tools after {MAX_TOOL_ROUNDS} rounds")
        except Exception as e:
            yield Failed(str(e))
        finally:
            self.total_usage.add(turn.usage)
