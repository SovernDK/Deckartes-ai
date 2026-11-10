# Telegram bot (`telegram_bot.py`, `telegram_api.py`)

A headless front end for `Conversation`, run as its own process with its own conversation. It doesn't import Textual. The bot is `@deckartes_bot`.

**Network.** The bot only makes outbound requests to `api.telegram.org` (long polling), so it opens no port and the phone doesn't need Tailscale. The Mac needs Tailscale for the Glances tools (`thinkpad`). Telegram bot chats aren't end-to-end encrypted: questions and replies pass through Telegram's servers.

**Setup.** `.env` needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID` (your numeric id, as `from.id` in `https://api.telegram.org/bot<token>/getUpdates` after messaging the bot). The bot refuses to start without them, with exit status 2.

**Running.** Open `deckartes-telegram.command`, which runs the bot in a Terminal window under `caffeinate -is` and restarts it after a crash, but not after a clean stop or a missing setting. To start it at login, add that file in System Settings > General > Login Items. Closing the window stops the bot. The log is `~/.cache/deckartes/telegram.log`.

It has to be Terminal. Tested on this Mac, a launchd LaunchAgent was refused calendar access instantly with the status left undetermined, and no prompt appeared, while the same check from Terminal saw every calendar. Folders worked from both.

**Staying awake.** `caffeinate -is` prevents idle sleep on power and battery, but closing the lid still sleeps the Mac unless it's in clamshell mode (power and external display). To run with the lid closed and on battery, run `sudo pmset -a disablesleep 1` (`pmset -g | grep SleepDisabled` shows 1; `sudo pmset -a disablesleep 0` undoes it). The setting is undocumented, isn't in `man pmset`, and needs turning off before the Mac goes in a bag, because it won't sleep there and can overheat. Telegram keeps messages for 24 hours, so those sent during sleep are answered on waking.

**Morning anime digest (`anime_digest.py`).** A short script, separate from the bot, that sends the episodes airing today from the shows on the user's list with a `config.anime_digest_statuses` status. It is fixed text with no model call, and an empty day sends nothing. `sendMessage` doesn't conflict with the bot's polling, so the two run side by side and the digest works when the bot is down.

launchd runs it from `launchd/com.deckartes.anime-digest.plist`. It can be a launchd job because it doesn't touch the calendar; a future job that needs Apple Calendar has to run inside the bot. A `StartCalendarInterval` job missed during sleep runs on wake, and several missed ones count as one. If the Mac is off at that time the day is skipped. Install or apply a changed time with:

```bash
launchctl bootout gui/$(id -u)/com.deckartes.anime-digest   # only when replacing it
cp launchd/com.deckartes.anime-digest.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deckartes.anime-digest.plist
launchctl kickstart gui/$(id -u)/com.deckartes.anime-digest  # run it now
```

launchd doesn't re-run a failed job, and the network can be down just after waking, so the script retries every 5 minutes for an hour and then exits 1. The log is `~/.cache/deckartes/anime-digest.log`, and a failure appears only there. The root logger stays at WARNING because httpx's INFO lines print the token.

**Access.** Only private chats from `config.telegram_user_id` are handled, checked on messages and button presses. Anything else is dropped with no reply, and only the sender's numeric id is logged.

**Polling.** `poll_once` moves the offset on before handling an update, so a crash can't repeat a turn or a calendar add. A turn runs as its own task, which leaves the loop free for button presses and `/stop`. Only one turn runs at a time, and a question during one gets the `BUSY` reply.

**Replies.** `typing` is re-sent every 4 s (it lasts at most 5 s), paused while a question waits for an answer. The complete reply is sent as plain text with no `parse_mode`, so a stray model character can't make Telegram reject it. `clean_markdown` removes code fences, headings and `**`. `split_text` cuts at 4,000 characters, on a paragraph, line or word when one is in the second half of the part. A `Failed` turn sends any partial reply followed by `Error: <message>`.

**Confirmations.** `Bot.run` sets `tools.confirm_handler` before the first poll, and clears it only after any running turn has ended, because with `None` every change is approved. `Bot.confirm` sends the question with Yes and No buttons whose data holds a random per-question id, so buttons left from before a restart read as expired. No answer in `CONFIRM_SECONDS` counts as No. If the question can't be sent the tool fails, so nothing changes.

**Commands.** `/help` (also `/start` and anything unknown), `/clear` (refused during a turn), `/stop`.

**The token** is part of every URL. `telegram_api._call` removes it from error text and drops the exception's cause, because that text reaches the log and, through a failed tool, the model. `setup_logging` keeps `httpx` at WARNING, since its INFO lines print full URLs. Message text is never logged.

**Prompt.** `run` sets `config.system_prompt` to `config.telegram_prompt`, the `Phone` preset, which affects only this process.

**Alongside the terminal app.** Both share one model server. While the bot's process owns it, the terminal app's model picker can't restart it (`mlx_server._Server.stop` only stops its own process), so stop the bot before changing models. A server the terminal app started stops when that app exits, which fails a bot turn in progress, and the next turn starts it again. History is in memory, so restarting the bot forgets the conversation.

**Tests.** `tests/test_telegram_bot.py` fakes only Telegram's HTTP (`http_fake.HttpTest`) and `mlx_client.stream_chat`. A fake poll never yields to the event loop, so to end `run` it raises `CancelledError` instead of calling `task.cancel()`. A fake model doesn't yield either, so a test that needs the typing task uses `block_model`.
