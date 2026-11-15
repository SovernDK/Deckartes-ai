# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What this is

Deckartes is a terminal chat UI (Textual) for local MLX models served by Rapid-MLX, with tool calling: the model can query a home server's Glances API, scan the local disk, search the web through Ollama's web API, look up anime seasons, timetables and ratings on AnimeSchedule.net, get Counter-Strike esports teams, players and matches from PandaScore plus Valve's ranking, read and add events in Apple Calendar, list the user's bank transactions through Enable Banking and total their spending per category, compared with earlier weeks and months, and search the files the user saved to its library.

## Project overview

- **Name:** Deckartes
- **Purpose:** a terminal chat UI and Telegram bot for local MLX models, with tool calling.
- **Stack:** Python 3.11+, Textual 8, httpx, Rapid-MLX on macOS (Apple Silicon), SQLite, launchd jobs.
- **Package manager:** pip, into `venv/`. There is no requirements file; check `venv/bin/pip list` for what's installed.

### Key commands

```bash
venv/bin/python src/main.py             # run the app
./deckartes-telegram.command            # run the Telegram bot (in Terminal, see docs/telegram.md)
(cd src && ../venv/bin/python -m jobs.anime_digest)   # send today's anime digest now (launchd runs it at 09:00)
(cd src && ../venv/bin/python -m jobs.schedule)       # apply the jobs list (jobs/schedule.py) to launchd
(cd src && ../venv/bin/python -m tools.enable_banking_api)   # link a bank in a browser (bank tools); --sandbox for a mock bank
PYTHONPATH=src venv/bin/textual run --dev src/main.py   # run with devtools; pair with `venv/bin/textual console` in another terminal to see logs/print output
PYTHONPATH=src venv/bin/python -m unittest discover tests   # tests; live tests skip unless LIVE=1
LIVE=1 PYTHONPATH=src:tests venv/bin/python -m unittest discover tests -p 'test_live_*.py'   # live tests only, on the running chat server
venv/bin/ruff check .                   # lint; add --fix for the automatic fixes
```
---

## Comments

- Write self-explanatory code; do not comment what the code already says.
- Comment only the **why**: non-obvious decisions, workarounds, business rules, links to issues/specs.
- No commented-out code, no changelog-style comments ("added by…", "fixed…"), no section banners.
- Docstrings only for exported/public symbols, and only when the signature isn't enough.

## Prefer existing libraries over custom code

- Before writing a utility, check (in this order):
  1. The standard library / platform APIs.
  2. Dependencies already in the project (`venv/bin/pip list`).
  3. Well-maintained, widely used libraries.
- Build a custom solution only when no suitable option exists, or the dependency cost (size, licence, maintenance) clearly outweighs the benefit. State the reason in your summary.
- Do not add a new dependency without mentioning it explicitly in your summary. Never add two libraries that solve the same problem.

## Testing

### Unit tests
- **Always load the `writing-tests` skill before writing or changing unit tests** and follow it.
- Every new feature or bug fix comes with unit tests; a bug fix starts with a failing test that reproduces it.
- Run `PYTHONPATH=src venv/bin/python -m unittest discover tests` before declaring a task done.

### UI and live tests
- UI flows are tested headlessly with Textual's pilot (recipe in [docs/ui.md](docs/ui.md)), in `tests/test_<feature>.py`; live tests against the running chat server go in `tests/test_live_<feature>.py` and skip unless `LIVE=1`.
- Test **user flows**, not implementation details; query widgets by id or type, not by CSS classes or tree position.
- No fixed sleeps; wait with `pilot.pause()` and assertions.
- Each test is independent and sets up and cleans up its own data (tests that mount `Chat` call `temp_history(self)`).
- Cover the happy path plus key failure paths (tool errors, failed turns, empty results).

## BUG Fixing

- ALWAYS fix root cause of the bugs, DONT create workarounds.

## Input validation

- **Validate all outside data** at the boundary: tool arguments from the model, Telegram messages, files saved to the library, env vars, CLI args and data from external APIs.
- Tool arguments are checked against the JSON schema built from the tool's docstring (`model/tool_schema.py`); parse dates with `clock.parse_date`.
- Whitelist, don't blacklist: define allowed types, lengths, ranges and formats.
- Telegram access is decided by `TELEGRAM_USER_ID` only, never by message content.
- Use parameterized SQLite queries; never build SQL or shell commands from raw input.
- Return clear error messages; never leak stack traces or internals.

## Git and commit policy

- **Branch:** work on `main`.
- **Commits:** an imperative summary sentence, ≤72 chars, saying what changed and why (e.g. "Label spending payees with a decision model instead of the chat model").
- One logical change per commit; keep commits small and buildable.
- Before committing: lint, Pylance diagnostics and tests must pass.
- Never commit secrets, `.env` files, credentials or generated build artifacts.
- **Do not** commit, push, rebase, force-push, reset, or delete branches unless explicitly asked.
- Never rewrite history on shared branches. Never use `--no-verify`.
- PRs: short description of *what* and *why*, linked issue (`#123`), notes on testing and any new dependencies.

## VS Code MCP tools

- When the VS Code MCP server is available, **prefer it over manual edits / grep** for:
  - **Refactoring:** rename symbol, extract function/variable, move file, organize imports — so all references update safely.
  - **Diagnostics:** fetch errors/warnings from the language server after every change and fix them before finishing.
  - **Navigation:** go to definition, find references, find implementations.
- Fall back to manual edits only when no MCP tool covers the operation.
- A task is not done while the MCP diagnostics report errors in files you touched.

## Code conventions

- **Always follow the `python-conventions` skill** for naming and style. Load it before writing code.
- If the skill and existing code disagree, follow the skill and flag the inconsistency instead of silently spreading the old pattern.
- No formatter is enforced yet (Ruff is used for lint only); match the surrounding style and don't run `ruff format` over the repo.
- Never hardcode values that someone might want to change without editing code. Put them in one central configuration location and read them from there.

## Wiki

- Save project architecture and use instructions in /docs/wiki.html
use /project-wiki skill

---

## Definition of done

- [ ] Lint, Pylance diagnostics and all tests pass
- [ ] No diagnostics errors in touched files (VS Code MCP)
- [ ] New/changed logic covered by unit tests; UI flows covered by pilot tests where relevant
- [ ] No unnecessary comments, no dead code
- [ ] Summary lists any new dependencies and open questions

## Reference docs

Read the matching doc before working in that area:

- [docs/model-server.md](docs/model-server.md): the Rapid-MLX servers, their flags, the prompt cache, or changing the chat or embedding model.
- [docs/adding-a-tool.md](docs/adding-a-tool.md): adding or changing a tool or the text it returns (registration, schema limits, exact-text tool tests, the `confirm` parameter for tools with side effects, `run_tool`).
- [docs/http-client.md](docs/http-client.md): adding or changing a web backend (`http_client.request`, error messages, caching, paging, `MockTransport` tests).
- [docs/tool-routing.md](docs/tool-routing.md): changing `tool_router.py`, its examples or thresholds, or debugging a wrong tool choice.
- [docs/tools-server.md](docs/tools-server.md): Glances tools.
- [docs/tools-anime.md](docs/tools-anime.md): AnimeSchedule tools.
- [docs/tools-cs.md](docs/tools-cs.md): Counter-Strike tools, PandaScore and Valve ranking.
- [docs/tools-banking.md](docs/tools-banking.md): the bank transactions tool, Enable Banking, signing requests and linking a bank, and spending categories with their digests (`spending.py`).
- [docs/tools-calendar.md](docs/tools-calendar.md): Apple Calendar tools. [docs/calendar-tests.md](docs/calendar-tests.md) has the fakes, because calendar access can't be tested from Claude Code's shell.
- [docs/library.md](docs/library.md): saving files and folders, chunking, embeddings, overwriting and searching (`library.py`).
- [docs/telegram.md](docs/telegram.md): the Telegram bot, its setup, access check, confirmations and how it runs, and the scheduled jobs (`jobs/`).
- [docs/validation.md](docs/validation.md): the `/validate` command, its case file, scoring and saved results.
- [docs/ui.md](docs/ui.md): widgets, screens, bindings, styles, the config popup, Default/Detailed mode, thinking and the usage line, plus the headless pilot recipe for verifying UI changes.

All architecture is kept in docs/wiki.html for human developers, any bigger change in logic should be updated there.