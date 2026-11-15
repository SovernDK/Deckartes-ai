"""Sends today's airing anime from the user's list to Telegram, then exits.

launchd runs it every morning (listed in schedule.py), separately from the bot, which it doesn't
need. Details are in docs/telegram.md.
"""

import sys

from core.config import config
from jobs import runner
from tools import animeschedule_api
from tools.animeschedule_api import AnimeScheduleError


async def run() -> int:
    return await runner.run(
        animeschedule_api.airing_digest,
        settings={"ANIMESCHEDULE_TOKEN": config.animeschedule_token},
        errors=(AnimeScheduleError,),
    )


if __name__ == "__main__":
    sys.exit(runner.main(run))
