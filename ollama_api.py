import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import ollama

from config import config
from tool_router import select_tools
from tools import run_tool

_clients = {}

def get_client() -> ollama.Client:
    """Sync client for the current config.host, recreated if the host changes."""
    if ("sync", config.host) not in _clients:
        _clients[("sync", config.host)] = ollama.Client(host=config.host)
    return _clients[("sync", config.host)]

def get_async_client() -> ollama.AsyncClient:
    """Async client for the current config.host, recreated if the host changes."""
    if ("async", config.host) not in _clients:
        _clients[("async", config.host)] = ollama.AsyncClient(host=config.host)
    return _clients[("async", config.host)]

ERROR_PREFIX = "**Error:**"

def error_message(e) -> str:
    return f"{ERROR_PREFIX} {e}"

def is_error(text: str) -> bool:
    return text.startswith(ERROR_PREFIX)

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
    # Tool groups the router sent with the question; None when it didn't run
    tool_groups: list[str] | None = None
    # What was sent from the loaded file: "whole" or "5 excerpts"; None without a file
    file_context: str | None = None

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

# Called with a tool's name before it runs, and with None when the model's tools are done
ToolCallback = Callable[[str | None], None] | None

async def run_tools(tool_calls, on_tool: ToolCallback) -> list[str]:
    results = []
    for call in tool_calls:
        if on_tool:
            on_tool(call.function.name)
        results.append(await run_tool(call.function.name, call.function.arguments))
    if on_tool:
        on_tool(None)
    return results

async def stream_response(messages, on_tool: ToolCallback = None, usage: Usage | None = None):
    """Yield the reply text chunk by chunk, running any tools the model calls;
    on failure yield an error message. Tokens used are added to usage."""
    messages = list(messages)
    try:
        tools = None
        if await supports_tools():
            selection = await select_tools(messages)
            tools = selection.tools
            if usage is not None:
                usage.tool_groups = selection.groups
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
            async for chunk in stream:
                if chunk.message.content:
                    content += chunk.message.content
                    yield chunk.message.content
                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)
                if chunk.done and usage is not None:
                    usage.count(chunk)

            if not tool_calls:
                return
            if content:
                yield "\n\n"
            results = await run_tools(tool_calls, on_tool)
            add_tool_results(messages, content, tool_calls, results)

        yield error_message(f"model still calling tools after {MAX_TOOL_ROUNDS} rounds")
    except Exception as e:
        yield error_message(e)

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
