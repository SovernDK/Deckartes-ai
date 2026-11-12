# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Deckartes is a terminal chat UI (Textual) for local MLX models served by Rapid-MLX, with tool calling: the model can query a home server's Glances API, scan the local disk, search the web through Ollama's web API, look up anime seasons, timetables and ratings on AnimeSchedule.net, get Counter-Strike esports teams, players and matches from PandaScore plus Valve's ranking, read and add events in Apple Calendar, and search the files the user saved to its library.

## TODO

[ ] Better RAG search (not just by embedded chunks)
[ ] Scanning user1 folder on homeserver
[ ] Prepare fine tuning data
[ ] Telegram: send files from the iPhone to the library (bots download up to 20 MB; files need a permanent inbox folder, since the library identifies them by path)
[ ] Telegram: live reply preview with `sendMessageDraft`

## Commands

There is no build step or requirements file. Tests use stdlib `unittest` in `tests/`. Linting uses Ruff, configured in `pyproject.toml`. Dependencies live in `venv/` (textual 8.2.8, textual-dev, httpx, pyobjc-framework-EventKit 12.2.2, pypdf 6.20.1, cryptography 50.0.2 for AES-encrypted PDFs, numpy 2.4.6, telegramify-markdown 1.4.2 for Telegram formatting, ruff 0.17.4).

```bash
venv/bin/python src/main.py             # run the app
./deckartes-telegram.command            # run the Telegram bot (in Terminal, see docs/telegram.md)
(cd src && ../venv/bin/python -m jobs.anime_digest)   # send today's anime digest now (launchd runs it at 09:00)
PYTHONPATH=src venv/bin/textual run --dev src/main.py   # run with devtools; pair with `venv/bin/textual console` in another terminal to see logs/print output
PYTHONPATH=src venv/bin/python -m unittest discover tests   # tests; live tests skip unless LIVE=1
LIVE=1 PYTHONPATH=src:tests venv/bin/python -m unittest discover tests -p 'test_live_*.py'   # live tests only, on the running chat server
venv/bin/ruff check .                   # lint; add --fix for the automatic fixes
```

Runtime requirements: Rapid-MLX at `config.mlx_server_command` (the app starts it and downloads the models on first use), Glances on `http://thinkpad:61208` (only for the server tools), `OLLAMA_API_KEY` (web tools) `ANIMESCHEDULE_TOKEN` (anime tools) and `PANDA_SCORE_API_TOKEN` (CS tools) in the environment or `.env`, macOS calendar access for the terminal app (calendar tools), and `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID` (Telegram bot).

Verify UI changes headlessly with Textual's pilot instead of launching the app: recipe in [docs/ui.md](docs/ui.md).

The `vscode-mcp` tools expose Pylance and Ruff for diagnostics, references, symbol info and renames; show a file in a tab first (`open_files` with `showEditor: true`), because `get_diagnostics` reports a background-only file as clean.

## Architecture

Python lives in `src/`, in one package per feature: `core` (config, clock, names, similarity, http_client), `model` (mlx_client, mlx_server, tool_schema), `agent` (conversation, history, prompts, tool_router), `tools` (the registry `tools.py`, its groups `tool_groups.py` and its backends), `ui` (the Textual app), `telegram`, `validation` and `jobs` (scheduled scripts). `src` is the import root (`from core.config import config`, `from tools import tools`), so scripts inside a package run with `-m` from `src/`. Data files (`.env`, `prompts.json`, `validation_cases.json`, `chat.tcss`) stay at the repo root, found through `config.PROJECT_ROOT`; tests stay in `tests/`. Modules are named below without their folder. `main.py` → `app.py` (`DecartesApp`: bindings, model-picker, help and file-explorer screens) → `chat.py` (widgets: bubbles, spinner, busy state, interrupt) → `conversation.py` (`Conversation`: history, the whole turn including the tool loop, no UI) → `tool_router.py` (picks tool groups per question; `tool_groups.py` says which tools go with them) → `tools.py` (tool registry) → `glances_api.py` / `disk_scan.py` / `web_search.py` / `animeschedule_api.py` / `cs_api.py` / `calendar_api.py` / `library.py` (tool backends; `library.py` also takes the explorer's loads and owns the saved files); the HTTP ones go through `http_client.py`). `mlx_client.py` is the only module that talks to the model servers, and `mlx_server.py` starts and stops them. `clock.py` is the one owner of the current time, of conversions to `config.timezone` and of parsing the days users name (`parse_day`), and `names.py` of normalising names for matching. `validation.py` runs `/validate` (no UI; `validation_screen.py` is its popup). `telegram_bot.py` is a second, headless front end for `Conversation`, run as its own process, `telegram_api.py` holds its Bot API calls and `telegram_format.py` turns replies into Telegram messages. `anime_digest.py` is a short script launchd runs each morning to message the user the day's anime, with no model and without the bot.

**Shared config.** `config.py` exposes a single mutable `config` instance (host, model, context_length, temperature, system_prompt, plus tool settings and keys). Modules read it at call time rather than taking parameters; widgets change settings by assigning to it (e.g. the model picker sets `config.model`). `context_length` is the app's own budget for compaction; the server doesn't cut prompts to it.

**Prompt presets.** `prompts.json` holds presets (name, description, `prompt`, `tools`: `"all"`, a list of tool names, or `[]`), selected by `config.system_prompt`. `prompts.py` re-reads the file on every use, so edits apply without a restart. `system_message()` always starts with the current local date, even when no preset matches, since models don't know it otherwise, and `current_time_line()` goes just before each question to keep the prompt cache valid. Presets shouldn't repeat either. `Conversation` puts the system message first on every turn (it isn't stored in the history), and `tool_router.select_tools` sends only the tools the preset allows. A new tool must also be added to the relevant presets, unless they use `"all"`. `allowed_tool_names` raises for a name that isn't a tool, so a typo shows as an error in the reply (and fails `tests/test_tools.py`) instead of silently dropping the tool.

**Turn events.** `Conversation.stream_reply` yields events to `Chat`: `Text` (reply text), `Thinking` (the model's reasoning: shown only in the app's Detailed mode, never saved, sent back or sent to Telegram), `ToolStarted(name, arguments)` / `ToolsDone` (drive the spinner label; Detailed mode also lists the calls), `NotSaved(message)` (the finished turn couldn't be written to the saved history; the app warns, the bot logs) and `Failed(message)`. It never raises: any failure is a `Failed`, always last. A turn with a `Failed` isn't saved; `Chat` shows it as a `**Error:**` paragraph. `tests/test_conversation.py` drives whole turns with only `mlx_client`'s chat calls faked. Tool failures are different: `tools.run_tool` turns exceptions into a text result that goes back to the model, not the user.

**Tool loop.** `stream_reply` sends the tools `tool_router.select_tools` picks, run any tool calls, append assistant + `role: tool` messages to a local copy of `messages`, and repeat up to `MAX_TOOL_ROUNDS`. Text before a tool round is followed by `Text("\n\n")`, so rounds are separate paragraphs in the saved reply.

**Conversation (`conversation.py`).** `Conversation.history` holds only finished turns (time line, question, reply text); tool calls and results aren't kept between turns. `stream_reply` yields the turn's events and saves the turn only after a clean finish, so a failed, cancelled or closed turn never reaches the history. There is no non-streaming path: a turn always runs as the `fetch_stream` worker on the app's loop. **Compaction:** when a finished turn's context (last prompt + reply tokens) reaches `config.compact_at` (share of `context_length`, `None` = off), the next turn first yields `Compacting`, replaces the whole history with the model's summary (one `system` message) and yields `Compacted`; a failed summary is a `Failed` and keeps the history. Model-calling logic goes here, not in widgets.

**Saved conversations (`history.py`).** `Conversation(source="app" | "telegram")` also saves each finished turn to `config.history_path` (SQLite), and a compaction summary as a `system` message without dropping what it replaced; no source (`/validate`) saves nothing. `clear()` ends the conversation, so the next turn starts a new one; `open(id)` continues a saved one, sending the model the last summary and what follows it. The app's history panel (key `r`, `history_panel.py`) lists, opens and deletes them. Details in [docs/ui.md](docs/ui.md); tests that mount `Chat` must call `temp_history(self)`.

## Git

Commit any changes unless user specifiec otherwise if:
- You finished new feature
- You fixed a bug
- You optimized something
Always commit on main unless user specifiec otherwise.

## Reference docs

Read the matching doc before working in that area:

- [docs/model-server.md](docs/model-server.md): the Rapid-MLX servers, their flags, the prompt cache, or changing the chat or embedding model.
- [docs/adding-a-tool.md](docs/adding-a-tool.md): adding or changing a tool or the text it returns (registration, schema limits, exact-text tool tests, the `confirm` parameter for tools with side effects, `run_tool`).
- [docs/http-client.md](docs/http-client.md): adding or changing a web backend (`http_client.request`, error messages, caching, paging, `MockTransport` tests).
- [docs/tool-routing.md](docs/tool-routing.md): changing `tool_router.py`, its examples or thresholds, or debugging a wrong tool choice.
- [docs/tools-server.md](docs/tools-server.md): Glances tools.
- [docs/tools-anime.md](docs/tools-anime.md): AnimeSchedule tools.
- [docs/tools-cs.md](docs/tools-cs.md): Counter-Strike tools, PandaScore and Valve ranking.
- [docs/tools-calendar.md](docs/tools-calendar.md): Apple Calendar tools. [docs/calendar-tests.md](docs/calendar-tests.md) has the fakes, because calendar access can't be tested from Claude Code's shell.
- [docs/library.md](docs/library.md): saving files and folders, chunking, embeddings, overwriting and searching (`library.py`).
- [docs/telegram.md](docs/telegram.md): the Telegram bot, its setup, access check, confirmations and how it runs.
- [docs/validation.md](docs/validation.md): the `/validate` command, its case file, scoring and saved results.
- [docs/ui.md](docs/ui.md): widgets, screens, bindings, styles, the config popup, Default/Detailed mode, thinking and the usage line, plus the headless pilot recipe for verifying UI changes.

All architecture is kept in docs/wiki.html for human developers, any bigger change in logic should be updated there.