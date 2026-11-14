# Deckartes

<p align="center">
  <img src="screnshots/1.png" alt="Chat answering questions about the home server" width="800">
</p>
<p align="center">
  <img src="screnshots/2.png" alt="Config popup with system prompt, temperature and context limit" width="395">
  <img src="screnshots/3.png" alt="File explorer for saving files to the library" width="395">
</p>

A personal assistant that runs on your Mac, in the terminal or on your phone through Telegram.

## Philosophy

- **Local first.** The model runs on Apple Silicon with [Rapid-MLX](https://github.com/raullenchai/Rapid-MLX). Your questions and files stay on the Mac. Only tool calls to web APIs leave it.
- **Small model, good tools.** A 1.7B model doesn't know today's date, your server or your calendar. Tools give it fresh, short, plain-text facts, so it answers from data instead of guessing.
- **Only the tools a question needs.** A router picks the relevant tool groups per question, which keeps prompts short and choices easy for a small model.
- **You approve every change.** Anything that changes the world outside the app, like adding a calendar event, asks first.

## Overview

A chat app built with [Textual](https://textual.textualize.io/). The model can call tools for:

- **Home server:** CPU, RAM, disks, temperatures and Docker containers, through Glances.
- **This computer:** folders, file search, folder sizes.
- **Web:** search and page reading, through Ollama's web API.
- **Anime:** seasons, ratings, airing times and your list, from AnimeSchedule.net.
- **Counter-Strike:** teams, players, matches and Valve's ranking, from PandaScore.
- **Calendar:** read, search and add events in Apple Calendar.
- **Bank:** your transactions for a date range, through Enable Banking.
- **Your files:** a library of saved documents, searched with embeddings.

Long conversations are summarised automatically when they near the context limit.

## Setup

You need an Apple Silicon Mac, Python 3.11+ and Rapid-MLX:

```bash
uv tool install 'rapid-mlx[embeddings]'
python3 -m venv venv
venv/bin/pip install textual textual-dev httpx pyobjc-framework-EventKit pypdf cryptography numpy pyjwt
```

The app starts the model servers itself and downloads models on first use.

Put API keys in `.env` in the repo folder. A tool without its key reports an error; the rest keep working.

```bash
OLLAMA_API_KEY=...          # web tools
ANIMESCHEDULE_TOKEN=...     # anime tools
PANDA_SCORE_API_TOKEN=...   # Counter-Strike tools (free plan works)
ENABLE_BANKING_APP_ID=...   # bank tools
ENABLE_BANKING_KEY_PATH=... # bank tools: the application's private key (.pem)
TELEGRAM_BOT_TOKEN=...      # Telegram bot
TELEGRAM_USER_ID=...        # your numeric Telegram id
```

The server tools need [Glances](https://nicolargo.github.io/glances/) in web server mode on the home server (`http://thinkpad:61208` by default). The calendar tools ask macOS for calendar access on first use. The bank tools need your bank linked once, in a browser: run `../venv/bin/python -m tools.enable_banking_api` from `src/` ([docs/tools-banking.md](docs/tools-banking.md)).

## Usage

```bash
venv/bin/python src/main.py
```

| Key | Action |
|---|---|
| `Enter` | Send |
| `Esc` | Leave the input box, so the single-key shortcuts work |
| `c` | Settings: prompt, temperature, context limit |
| `m` | Change model |
| `e` | File explorer, to save files to the library |
| `shift+t` | Detailed mode: thinking, tool calls and token usage |
| `h` | Help |
| `q` | Quit |
| `ctrl+c` | Stop the reply, or copy selected text |

| Command | Action |
|---|---|
| `/clear` | Clear the conversation |
| `/kill` | Stop the model servers to free memory |
| `/library` | List saved files |
| `/forget <path or name>` | Remove files from the library |
| `/validate` | Score the model's tool use on test cases |

## Library

Press `e`, then `Enter` on a file or `l` on a file or folder to save it. Text files, code and PDFs with a text layer work. Saved files stay between sessions, and answers name the file and the page or line.

Loading a folder again only embeds new or changed files. Small models sometimes answer from their own knowledge, so check answers that matter. Details: [docs/library.md](docs/library.md).

## Telegram

A Telegram bot gives the same assistant on your phone. It answers only your user id and asks before calendar changes with Yes and No buttons. Start it with `deckartes-telegram.command`, and add that file to Login Items to start it at login. It must run from Terminal, because background services are refused calendar access.

Telegram chats aren't end-to-end encrypted, so questions and replies pass through Telegram's servers.

Separate morning jobs, run by launchd, send the day's anime episodes from your list and remind you before the bank link expires. `src/jobs/schedule.py` lists them and when they run.

Details: [docs/telegram.md](docs/telegram.md).

## Tool routing

Tool definitions cost about 2,700 tokens per question. Before each question, the router compares it with example questions per tool group and sends only the groups that match. Small talk gets no tools, and vague follow-ups reuse the previous topic. To fix a wrong pick, add an example question to the right group. Details: [docs/tool-routing.md](docs/tool-routing.md).

## Configuration

Settings live in `src/core/config.py`. The settings popup (`c`) and model picker (`m`) change some of them while the app runs. The system prompt is in `prompts.json` and is re-read on every message.

## Development

```bash
PYTHONPATH=src venv/bin/python -m unittest discover tests   # tests
venv/bin/ruff check .                                       # lint
PYTHONPATH=src venv/bin/textual run --dev src/main.py       # with devtools; logs in `venv/bin/textual console`
```

Code lives in `src/`, one package per feature: `ui`, `agent`, `model`, `tools`, `telegram`, `core`, `validation` and `jobs`.

- Adding a tool: [docs/adding-a-tool.md](docs/adding-a-tool.md)
- Architecture: [CLAUDE.md](CLAUDE.md) and [docs/wiki.html](docs/wiki.html)
- Plans: [FINE-TUNING.md](FINE-TUNING.md)
