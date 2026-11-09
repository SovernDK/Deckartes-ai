# Telegram integration

A plan for using Deckartes from an iPhone through a Telegram bot. The bot is built and working. How it works now is in `docs/telegram.md`.

## Context

Deckartes only runs as a terminal UI on the MacBook. The goal is to ask it questions from the iPhone, with the same tools, while the MacBook runs it as a background service.

Decisions made:

- It runs on this MacBook (M1 MacBook Pro).
- Replies show "typing…" and then arrive complete, with no live streaming.
- The first version is text only. Sending files to the library comes later.

### What the tailnet does here

Telegram doesn't travel over the tailnet. The bot makes outbound long-polling requests to `api.telegram.org`, so it opens no port, and needs no `tailscale serve` or Funnel. The iPhone doesn't need Tailscale for this.

The MacBook still needs Tailscale connected, because the server tools reach `thinkpad:61208` by its tailnet name.

**Privacy:** Telegram bot chats aren't end-to-end encrypted. Questions and replies pass through Telegram's servers, including any calendar, file or server details the model writes in a reply. Models and tools stay on the Mac.

## Design

A separate headless process, `telegram_bot.py`, with its own `Conversation`. It doesn't import Textual, and the terminal app keeps working as it does today.

```
iPhone ⇄ Telegram cloud ⇄ telegram_api.py (long poll, send) ⇄ telegram_bot.py
                                                                   ↓
                                  conversation.py → tools.py, mlx_client.py (unchanged)
```

`Conversation.stream_reply` already has no UI dependency, never raises, and keeps failed or cancelled turns out of the history, so the bot is a new front end and the turn logic doesn't change.

### New: `telegram_api.py`

Thin Bot API calls through `http_client.request` (`docs/http-client.md`), with its own `TelegramError`. No new dependency.

- `get_updates(offset, timeout=50)`, with `allowed_updates` of `message` and `callback_query`, and an HTTP timeout about 10 s longer than the poll.
- `send_message`, `send_chat_action`, `answer_callback`, `edit_message_text`, `set_my_commands`.
- `status_messages` gives clear errors for 401 (bad token) and 409 (another copy polling).

The token is part of the URL, so it must stay out of the logs. Set the `httpx` logger to WARNING, because its INFO lines print full URLs, and replace the token in any error text before logging it.

### New: `telegram_bot.py`

**Access.** The bot can be found by anyone on Telegram, and its tools read the disk and calendar. Only private chats from `config.telegram_user_id` are handled, checked on messages and on button presses. Everything else is dropped without a reply, logging only the numeric id. The bot refuses to start when the token or user id is missing.

**Poll loop.** Long-polls and retries with backoff up to 60 s, which also covers the Mac waking from sleep. A turn runs as its own task, so the loop stays free to receive button presses and `/stop`. The offset moves on when an update is received, so a crash can't repeat a calendar add. A message caught mid-crash is lost and has to be sent again. Messages sent while the Mac slept are answered on waking, for up to Telegram's 24-hour retention.

**One turn at a time.** A question during a turn gets "Still answering, /stop cancels it".

**A turn.**
- Sends `typing` every 4 s, since the status lasts at most 5 s, and pauses it while waiting for a confirmation.
- Collects `Text` events and sends the complete reply at the end.
- On `Failed`, sends any partial reply followed by `Error: <message>`.
- Sends a short note if the reply is empty.

**Reply text.** Plain text with no `parse_mode`, so a stray model character can't make Telegram reject the message. A small cleanup removes the common Markdown markers (`**`, headings, code fences). Replies are split at paragraph, line or word boundaries at 4,000 characters, under Telegram's 4,096 limit, with about 1 s between parts. Sends retry up to 3 times.

**Confirmations.** `tools.confirm_handler` must be set before polling starts, because when it is `None`, `tools.confirm` approves every change (`tools.py:23`). The handler sends the question with Yes and No buttons whose `callback_data` includes a per-question id, then awaits a future. No answer in 5 minutes counts as No. Buttons are always removed afterwards, every press gets `answerCallbackQuery`, and old buttons reply "Expired". `/stop` while asking cancels the question. If the question can't be sent, the error becomes a tool failure and nothing is added.

**Commands**, registered with `setMyCommands` so they appear in the iOS menu: `/start` and `/help`, `/clear` (refused during a turn), `/stop`.

**Prompt.** The bot sets `config.system_prompt = config.telegram_prompt`, which only affects its own process. A new preset in `prompts.json` has `"tools": "all"` and says the user is on a phone, so answers should be short with no tables.

**Startup and shutdown.** Starts `mlx_server.ensure_chat` in the background so a first-time model download doesn't block polling. A SIGTERM handler ends the loop cleanly, because Python otherwise skips `atexit`, and the Rapid-MLX servers run in their own session where launchd's stop can't reach them.

### Changed

- `config.py`: `telegram_token` (`TELEGRAM_BOT_TOKEN`, `repr=False`), `telegram_user_id` (`TELEGRAM_USER_ID`), `telegram_prompt`.
- `prompts.json`: the phone preset.

`conversation.py`, `tools.py`, `mlx_client.py`, `mlx_server.py` and `http_client.py` don't change.

### Running as a service

`deckartes-telegram.command`, added to Login Items, opens Terminal and runs `/usr/bin/caffeinate -s venv/bin/python telegram_bot.py` from the repo folder, restarting it after a crash. It logs to `~/.cache/deckartes/telegram.log`. Logs never contain message text.

`caffeinate -s` prevents sleep only on power, so the battery behaves normally. **With the lid closed the Mac still sleeps** unless it's in clamshell mode (power plus external display), and the bot is unreachable until it wakes.

## Risks to check first

1. **Calendar and folder permissions under launchd (checked).** A LaunchAgent was refused calendar access instantly, with no prompt. The same check from Terminal saw all 7 calendars. Documents, Desktop and Downloads worked from both. So the bot starts from Terminal through a Login Item and not from a LaunchAgent.
2. **Running alongside the terminal app.** Both share one model server. While the bot owns it, the terminal app's model picker can't restart it (`mlx_server.py:99-103`), so the bot has to be stopped before changing models. A server started by the terminal app stops when that app exits, which fails any bot turn in progress. The next turn starts it again.
3. **Restarts forget the conversation.** History is in memory, as in the terminal app. The library and calendar aren't affected.

## Steps

1. **Setup (by hand).** Create the bot with @BotFather, add `TELEGRAM_BOT_TOKEN` to `.env`, message the bot, read `from.id` from `https://api.telegram.org/bot<token>/getUpdates`, and add `TELEGRAM_USER_ID`.
2. **Permission check.** Done. Terminal through a Login Item was chosen.
3. **`config.py` and `telegram_api.py`**, test first.
4. **`telegram_bot.py`**, in this order: access check, turn and reply, commands and `/stop`, confirmations.
5. **Preset and launcher**, with install steps in the docs.
6. **Docs:** new `docs/telegram.md`, a line in `CLAUDE.md` (architecture, commands, runtime, reference docs, TODO item), the confirmation paragraph in `docs/adding-a-tool.md`, `README.md`, and `docs/wiki.html`.

## Verification

`tests/test_telegram_bot.py`, using `http_client._transport` with `MockTransport` (`tests/http_fake.py`) and only `mlx_client`'s chat calls faked, as in `tests/test_conversation.py`:

- Other users, group chats and other users' button presses are ignored.
- The bot won't start without a token or user id, and `tools.confirm_handler` is set before the first poll.
- A whole turn: question in, one complete reply out, saved in the history.
- Long replies split under the limit, on boundaries. Markdown cleanup.
- `Failed` keeps the partial reply and adds the error. A question during a turn gets the busy reply.
- Calendar add: Yes saves. No, timeout and `/stop` don't save. Old buttons do nothing.
- `/clear` and `/stop` behaviour. The offset moves on before the turn finishes. Polling recovers after an error.
- The token doesn't appear in logged error text.

Run `venv/bin/python -m unittest discover tests` and `venv/bin/ruff check .`.

Then on the iPhone, with the service running: ask a question that needs no tool, one for each of server, web and calendar, approve and decline a calendar add, `/stop` a long turn, send a long reply, and message the bot from a second Telegram account to confirm silence. Restart the Mac and confirm the bot comes back, then put it to sleep and confirm a queued message is answered on waking.

## Later

- Sending files from the iPhone to the library (bots can download up to 20 MB, and files need a permanent inbox folder, since the library identifies them by path).
- `/library` and `/forget`.
- Keeping the conversation across restarts.
- Real Telegram formatting (bold, code blocks).
- Live reply preview with `sendMessageDraft`.
