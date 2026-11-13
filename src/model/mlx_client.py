"""Chat and embeddings from the local Rapid-MLX servers, over their OpenAI-style HTTP API.

Every call makes sure its server is running first (mlx_server.py), so a server that crashed comes
back on the next turn. Failures raise ModelServerError with a readable message.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from core.config import config
from model import mlx_server
from model.mlx_server import ModelServerError
from model.tool_schema import tool_definition

# Tests set httpx.MockTransports here in place of the network
_transport: httpx.AsyncBaseTransport | None = None
_sync_transport: httpx.BaseTransport | None = None

# No read limit: a request may wait for a model to load, or generate for a long time
TIMEOUT = httpx.Timeout(None, connect=5.0)

@dataclass
class ToolCall:
    id: str
    name: str
    # None when the model's arguments weren't valid JSON
    arguments: dict | None

    def as_message(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.arguments or {})}}

@dataclass
class TokenCount:
    """What one chat call used. The prompt count includes tokens the server had cached."""
    prompt: int = 0
    completion: int = 0
    # Time spent generating the completion, without prompt evaluation; 0 when not reported
    generation_ns: int = 0

@dataclass
class Delta:
    """One piece of a streamed reply. Tool calls and the token count come in the last one."""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenCount | None = None
    # The model's thinking, which the server's reasoning parser keeps out of content
    reasoning: str = ""

@dataclass
class Reply:
    content: str
    usage: TokenCount | None

def _token_count(usage: dict | None, metrics: dict | None) -> TokenCount | None:
    if not usage:
        return None
    completion = usage.get("completion_tokens", 0)
    token_ms = (metrics or {}).get("mean_itl_ms") or 0
    return TokenCount(usage.get("prompt_tokens", 0), completion, round(completion * token_ms * 1e6))

def _chat_body(messages: list[dict], tools: list | None = None, thinking: bool | None = None) -> dict:
    body = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_tokens,
        "enable_thinking": config.enable_thinking if thinking is None else thinking,
    }
    for name in ("temperature", "top_k", "repetition_penalty"):
        if getattr(config, name) is not None:
            body[name] = getattr(config, name)
    if tools:
        body["tools"] = [tool_definition(tool) for tool in tools]
    return body

def _status_error(status: int, body: str) -> ModelServerError:
    try:
        detail = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = body[:300]
    return ModelServerError(f"The model server responded with status {status}: {detail}")

def _unreachable(url: str) -> ModelServerError:
    return ModelServerError(f"Could not reach the model server at {url}")

def _tool_call(parts: dict) -> ToolCall:
    try:
        arguments = json.loads(parts["arguments"] or "{}")
    except ValueError:
        arguments = None
    return ToolCall(parts["id"], parts["name"], arguments if isinstance(arguments, dict) else None)

def _add_tool_call_delta(calls: dict[int, dict], delta: dict) -> None:
    """Join the pieces of a tool call, which may arrive over several chunks."""
    call = calls.setdefault(delta.get("index", 0), {"id": "", "name": "", "arguments": ""})
    call["id"] = delta.get("id") or call["id"]
    function = delta.get("function") or {}
    call["name"] = function.get("name") or call["name"]
    arguments = function.get("arguments") or ""
    call["arguments"] += arguments if isinstance(arguments, str) else json.dumps(arguments)

async def stream_chat(messages: list[dict], tools: list | None = None) -> AsyncIterator[Delta]:
    """Stream a reply. Closing this generator closes the connection, which stops the generation."""
    await asyncio.to_thread(mlx_server.ensure_chat)
    body = {**_chat_body(messages, tools), "stream": True, "stream_options": {"include_usage": True}}
    calls: dict[int, dict] = {}
    usage = metrics = None
    try:
        async with (
            httpx.AsyncClient(transport=_transport, timeout=TIMEOUT) as client,
            client.stream("POST", f"{config.host}/v1/chat/completions", json=body) as response,
        ):
            if response.status_code != 200:
                raise _status_error(response.status_code, (await response.aread()).decode(errors="replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                data = json.loads(payload)
                if data.get("error"):
                    raise ModelServerError(f"The model server failed: {data['error'].get('message', data['error'])}")
                usage = data.get("usage") or usage
                metrics = data.get("metrics") or metrics
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    for call in delta.get("tool_calls") or []:
                        _add_tool_call_delta(calls, call)
                    # Rapid-MLX sends thinking as reasoning_content; other servers name it reasoning
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield Delta(reasoning=reasoning)
                    if delta.get("content"):
                        yield Delta(content=delta["content"])
    except httpx.TransportError as e:
        raise _unreachable(config.host) from e
    yield Delta(tool_calls=[_tool_call(calls[i]) for i in sorted(calls)], usage=_token_count(usage, metrics))

async def complete(messages: list[dict]) -> Reply:
    """A whole reply without tools or thinking, for compacting the conversation."""
    await asyncio.to_thread(mlx_server.ensure_chat)
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=TIMEOUT) as client:
            response = await client.post(f"{config.host}/v1/chat/completions",
                                         json=_chat_body(messages, thinking=False))
    except httpx.TransportError as e:
        raise _unreachable(config.host) from e
    if response.status_code != 200:
        raise _status_error(response.status_code, response.text)
    data = response.json()
    content = data["choices"][0]["message"].get("content") or ""
    return Reply(content, _token_count(data.get("usage"), data.get("metrics")))

def _vectors(response: httpx.Response) -> list[list[float]]:
    if response.status_code != 200:
        raise _status_error(response.status_code, response.text)
    return [item["embedding"] for item in sorted(response.json()["data"], key=lambda item: item["index"])]

async def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with config.embed_model, for tool routing."""
    await asyncio.to_thread(mlx_server.ensure_chat)
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=TIMEOUT) as client:
            response = await client.post(f"{config.host}/v1/embeddings",
                                         json={"model": config.embed_model, "input": texts})
    except httpx.TransportError as e:
        raise _unreachable(config.host) from e
    return _vectors(response)

def embed_file_text(texts: list[str]) -> list[list[float]]:
    """Embed texts with config.rag_embed_model, for the library. Blocks: call it in a thread."""
    mlx_server.ensure_files()
    try:
        with httpx.Client(transport=_sync_transport, timeout=TIMEOUT) as client:
            response = client.post(f"{config.file_embed_host}/v1/embeddings",
                                   json={"model": config.rag_embed_model, "input": texts})
    except httpx.TransportError as e:
        raise _unreachable(config.file_embed_host) from e
    return _vectors(response)

def _hub_folder() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"

def list_models() -> list[str]:
    """Chat models that can be picked: the current one and the complete ones in the Hugging Face cache."""
    found = {config.model}
    hidden = {config.embed_model, config.rag_embed_model}
    for repo in _hub_folder().glob("models--*"):
        name = repo.name.removeprefix("models--").replace("--", "/")
        if name not in hidden and "embed" not in name.lower() \
                and any(repo.glob("snapshots/*/config.json")) and any(repo.glob("snapshots/*/*.safetensors")):
            found.add(name)
    return sorted(found, key=str.lower)
