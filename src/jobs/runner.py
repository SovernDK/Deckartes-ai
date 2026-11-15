"""What every scheduled job shares: it checks its settings, sends one Telegram message to the
user, retries and exits with a code launchd logs.

A job (listed in schedule.py) is a module whose `run()` calls `runner.run(message, ...)`, where
message() builds the text to send, or None for nothing today.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from telegramify_markdown import split_entities

from core.config import config
from telegram import telegram_api
from telegram.telegram_api import TelegramError

log = logging.getLogger("deckartes.jobs")

# launchd doesn't re-run a failed job, and the network can be down just after waking
RETRY_SECONDS = 300
MAX_ATTEMPTS = 12
EXIT_FAILED = 1
EXIT_SETUP_ERROR = 2


async def _send(chat_id: int, text: str) -> None:
    # Plain text, not Markdown: titles and names can hold * or _
    for part, _ in split_entities(text, [], telegram_api.MAX_MESSAGE):
        await telegram_api.send_message(chat_id, part.rstrip())


async def run(
    message: Callable[[], Awaitable[str | None]],
    *,
    settings: dict[str, str] | None = None,
    errors: tuple[type[Exception], ...] = (),
    done: Callable[[], None] | None = None,
) -> int:
    """Send message() to the user, retrying on errors (and Telegram's) for an hour.
    settings maps each environment variable the job needs to its value in config. done() runs once
    the message was sent, or there was nothing to send, so what it records is never lost unsent."""
    needed = {
        "TELEGRAM_BOT_TOKEN": config.telegram_token,
        "TELEGRAM_USER_ID": config.telegram_user_id if config.telegram_user_id.isdigit() else "",
        **(settings or {}),
    }
    missing = [name for name, value in needed.items() if not value]
    if missing:
        log.error("Set %s in the environment or .env", ", ".join(missing))
        return EXIT_SETUP_ERROR
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = await message()
            if text is None:
                log.info("Nothing to send today")
            else:
                await _send(int(config.telegram_user_id), text)
                log.info("Sent the message")
            if done is not None:
                done()
            return 0
        except (*errors, TelegramError) as e:
            log.warning("Attempt %d of %d failed: %s", attempt, MAX_ATTEMPTS, e)
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(RETRY_SECONDS)
    log.error("Giving up for today")
    return EXIT_FAILED


def main(run_job: Callable[[], Coroutine[Any, Any, int]]) -> int:
    # The root stays at WARNING: httpx's INFO lines print full URLs and so the token
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    return asyncio.run(run_job())
