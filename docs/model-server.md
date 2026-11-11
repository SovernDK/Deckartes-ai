# Model server (`mlx_server.py`, `mlx_client.py`)

Local models run in Rapid-MLX (`uv tool install 'rapid-mlx[embeddings]'`), in its own environment. `mlx_server` starts it as child processes and `mlx_client` is the only module that talks to it. `ToolCall`, `Delta` and `TokenCount` are what the rest of the app sees. `tool_schema.py` builds the tool definitions.

**Two servers.** A Rapid-MLX server holds one embedding model and rejects any other name. The chat server (`config.host`, started on mount) holds the chat model and `config.embed_model`. The embeddings-only server (`config.file_embed_host`) holds `config.rag_embed_model` and starts the first time the library is used in a session, by a load or a search. Each call runs `ensure_chat` or `ensure_files`, so a crashed server returns on the next turn.

**Process rules.** A server already answering on a port is reused and never stopped. Logs are `~/.cache/deckartes/rapid-mlx-*.log`. Servers stop on exit through `atexit`; `/kill` stops them too, since there's no need for an unload call. The model is a launch argument, so `DecartesApp.set_model` restarts the chat server.

**Readiness.** Poll `/health/ready`, which is 503 until the model has loaded. `/v1/models` answers as soon as the port binds.

**Model flags.** LFM3's 4-bit build isn't in Rapid-MLX's catalog, so `PARSER_FLAGS` names its parsers: `nemotron` handles the `<tool_call><function=` format. A new model family that doesn't auto-detect gets an entry there. Thinking is set per request (`enable_thinking`): on for chat turns (`config.enable_thinking`), always off for `complete()`, the compaction summary. The `qwen3` reasoning parser streams it as `reasoning_content`, apart from `content`, and `stream_chat` passes it on as `Delta.reasoning`. It counts toward `max_tokens` (8,192). It is never sent back: LFM3's template drops earlier turns' thinking anyway, and Rapid-MLX's message format has no field for it. The `--no-thinking` flag is a different thing: it leaves thinking tokens in the reply.

**EmbeddingGemma 2** must be a catalog build (`-4bit` or `-bf16`). Rapid-MLX loads those natively, and the `-8bit` build fails with "Model type embedding_gemma2 not supported".

**Prompt cache.** The server reuses the longest identical start of the prompt: about 0.15s to first token against 3s cold for 1,300 tokens on an M1. Keep the start of the prompt stable. The date is at the top, the time goes just before each question and is saved in the history with its turn. A different set of routed tool groups is a cold start, because tools are part of the first system block.

**Usage.** `prompt_tokens` includes cached tokens. Speed comes from `metrics.mean_itl_ms` on the finish chunk.

**Tests.** `tests/test_mlx_client.py` uses `httpx.MockTransport` with SSE in the real server's shape, and `tests/test_mlx_server.py` runs a fake server program as the child. The live tests (`tests/test_live_*.py`) use the real server and run only with `LIVE=1` and the chat server up. Run them after changing the model, the flags or the message format.
