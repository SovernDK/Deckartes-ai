"""Sends today's airing anime from the user's list to Telegram, then exits.

launchd runs it every morning (launchd/com.deckartes.anime-digest.plist), separately from the
bot, which it doesn't need. Details are in docs/telegram.md.
"""

import asyncio
import logging
import sys

from core.config import config
from telegram import telegram_api
from telegram.telegram_api import TelegramError
from tools import animeschedule_api
from tools.animeschedule_api import AnimeScheduleError

log = logging.getLogger("deckartes.digest")

# launchd doesn't re-run a failed job, and the network can be down just after waking
RETRY_SECONDS = 300
MAX_ATTEMPTS = 12
EXIT_FAILED = 1
EXIT_SETUP_ERROR = 2

async def _send_digest(chat_id: int) -> bool:
    text = await animeschedule_api.airing_digest()
    if text is None:
        return False
    for part in telegram_api.split_text(text):
        await telegram_api.send_message(chat_id, part)
    return True

async def run() -> int:
    if not config.telegram_token or not config.telegram_user_id.isdigit() or not config.animeschedule_token:
        log.error("Set TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID and ANIMESCHEDULE_TOKEN in the environment or .env")
        return EXIT_SETUP_ERROR
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log.info("Sent the digest" if await _send_digest(int(config.telegram_user_id)) else "Nothing airs today")
            return 0
        except (AnimeScheduleError, TelegramError) as e:
            log.warning("Attempt %d of %d failed: %s", attempt, MAX_ATTEMPTS, e)
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(RETRY_SECONDS)
    log.error("Giving up on today's digest")
    return EXIT_FAILED

def main() -> int:
    # The root stays at WARNING: httpx's INFO lines print full URLs and so the token
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    return asyncio.run(run())

if __name__ == "__main__":
    sys.exit(main())
