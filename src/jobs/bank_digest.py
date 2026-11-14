"""Sends the user, on Telegram, the spending digest of each finished week (on Monday) and month (on
the 1st), and a reminder when the bank link expires in 7 days or 1 day, then exits.

launchd runs it every morning (listed in schedule.py), separately from the bot. Without a reminder
the bank tools would just stop working once the link expires. Details are in docs/tools-banking.md.
"""

import sys

from jobs import runner
from tools import enable_banking_api, spending
from tools.enable_banking_api import BankingError


async def run() -> int:
    # The digests in the message being sent, saved only once it's been sent
    sending: list[spending.Digest] = []

    async def message() -> str | None:
        sending[:] = await spending.due_digests() if enable_banking_api.has_live_link() else []
        parts = [enable_banking_api.link_reminder(), *(digest.text for digest in sending)]
        return "\n\n".join(part for part in parts if part) or None

    return await runner.run(message, errors=(BankingError,), done=lambda: spending.save(sending))

if __name__ == "__main__":
    sys.exit(runner.main(run))
