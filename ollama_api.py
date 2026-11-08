import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field

from config import config
from ollama_client import get_async_client
from tool_router import select_tools
from tools import run_tool

# What a turn yields (stream_response, then Conversation.stream_reply), in order
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

async def supports_tools() -> bool:
    """Whether the current model accepts tools; cached per host and model."""
    key = (config.host, config.model)
    if key not in _tool_support:
        info = await get_async_client().show(config.model)
        _tool_support[key] = "tools" in (info.capabilities or [])
    return _tool_support[key]

def model_options() -> dict:
    """Ollama options from config: context window and, unless left to the model, temperature."""
    options = {"num_ctx": config.context_length}
    if config.temperature is not None:
        options["temperature"] = config.temperature
    return options

def add_tool_results(messages, content, tool_calls, results) -> None:
    """Append the model's tool calls and their results so the next request sees them."""
    messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
    for call, result in zip(tool_calls, results):
        messages.append({"role": "tool", "tool_name": call.function.name, "content": result})

async def stream_response(messages, turn: TurnInfo | None = None) -> AsyncIterator[Event]:
    """Yield the reply as events, running any tools the model calls. Never raises: a failure
    is yielded as Failed, last. Tokens used are added to turn as each model call finishes."""
    messages = list(messages)
    turn = turn or TurnInfo()
    try:
        tools = None
        if await supports_tools():
            selection = await select_tools(messages)
            tools = selection.tools
            turn.tool_groups = selection.groups
        for _ in range(MAX_TOOL_ROUNDS):
            stream = await get_async_client().chat(
                model=config.model,
                messages=messages,
                tools=tools,
                options=model_options(),
                stream=True,
            )
            content = ""
            tool_calls = []
            # aclosing: a closed turn stops Ollama's request at once, not when Python gets to it
            async with aclosing(stream):
                async for chunk in stream:
                    if chunk.message.content:
                        content += chunk.message.content
                        yield Text(chunk.message.content)
                    if chunk.message.tool_calls:
                        tool_calls.extend(chunk.message.tool_calls)
                    if chunk.done:
                        turn.usage.count(chunk)

            if not tool_calls:
                return
            if content:
                yield Text("\n\n")
            results = []
            for call in tool_calls:
                yield ToolStarted(call.function.name)
                results.append(await run_tool(call.function.name, call.function.arguments))
            yield ToolsDone()
            add_tool_results(messages, content, tool_calls, results)

        yield Failed(f"model still calling tools after {MAX_TOOL_ROUNDS} rounds")
    except Exception as e:
        yield Failed(str(e))

async def fetch_models() -> list[str]:
    """Return the names of locally available models, or [] on failure."""
    try:
        response = await get_async_client().list()
    except Exception:
        return []

    return [model.model for model in response.models]

async def unload_models() -> tuple[list[str], str | None]:
    """Release every model Ollama has loaded in memory. Returns (unloaded names, error or None)."""
    unloaded = []
    try:
        running = await get_async_client().ps()
        for model in running.models:
            # An empty request with keep_alive=0 makes Ollama unload the model right away
            await get_async_client().generate(model=model.model, keep_alive=0)
            unloaded.append(model.model)
    except Exception as e:
        return unloaded, str(e)
    return unloaded, None
