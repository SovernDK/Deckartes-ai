"""Tells the user on Telegram when the bank link expires in 7 days or 1 day, then exits.

launchd runs it every morning (listed in schedule.py), separately from the bot. Without a reminder
the bank tools would just stop working once the link expires. Details are in docs/tools-banking.md.
"""

import sys

from jobs import runner
from tools import enable_banking_api


async def _message() -> str | None:
    return enable_banking_api.link_reminder()

async def run() -> int:
    return await runner.run(_message)

if __name__ == "__main__":
    sys.exit(runner.main(run))
