"""Deckartes through a Telegram bot: a headless front end for Conversation, run on its own.

Run it with deckartes-telegram.command, so it has Terminal's calendar permission. It only makes
outbound requests to Telegram and answers one person, config.telegram_user_id, in a private chat.
Details and setup are in docs/telegram.md.
"""

import asyncio
import logging
import logging.handlers
import secrets
import signal
import sys
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from agent.conversation import Conversation, Failed, Text
from core.config import config
from model import mlx_server
from telegram import telegram_api
from telegram.telegram_api import TelegramError, TelegramRejected
from telegram.telegram_format import Part, reply_parts
from tools import tools

log = logging.getLogger("deckartes.telegram")

LOG_FILE: Path | None = Path.home() / ".cache" / "deckartes" / "telegram.log"
# The typing status lasts at most 5 seconds
TYPING_SECONDS = 4
CONFIRM_SECONDS = 5 * 60
PART_PAUSE_SECONDS = 1
SEND_ATTEMPTS = 3
SEND_RETRY_SECONDS = 2
POLL_RETRY_SECONDS = 1
POLL_RETRY_LIMIT_SECONDS = 60
# The exit status that deckartes-telegram.command doesn't restart after
EXIT_SETUP_ERROR = 2

COMMANDS = {
    "help": "What this bot does",
    "clear": "Forget this conversation",
    "stop": "Stop the current answer",
}
HELP = ("Ask me anything, and I'll answer with the tools on your Mac.\n\n"
        "/clear forgets this conversation\n/stop stops the current answer")
BUSY = "Still answering, /stop cancels it"
TEXT_ONLY = "I can only read text messages for now."
EXPIRED = "Expired"
TIMED_OUT = "No answer after 5 minutes, so nothing was changed."

class SetupError(Exception):
    """The bot can't start. The message says what to set."""

@dataclass
class _Question:
    """A yes-or-no question waiting for a button press."""
    id: str
    answer: asyncio.Future

class Bot:
    def __init__(self, conversation: Conversation | None = None) -> None:
        if not config.telegram_token:
            raise SetupError("No bot token: set TELEGRAM_BOT_TOKEN in the environment or .env")
        if not config.telegram_user_id.isdigit():
            raise SetupError("No user to answer: set TELEGRAM_USER_ID to your numeric Telegram id in .env")
        self.owner = int(config.telegram_user_id)
        self.conversation = conversation or Conversation()
        self.offset: int | None = None
        self.turn: asyncio.Task | None = None
        self.pending: _Question | None = None

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()

    async def run(self) -> None:
        """Poll Telegram until cancelled."""
        # First: without a handler, tools.confirm approves every change
        tools.confirm_handler = self.confirm
        model = asyncio.create_task(self._start_model())
        try:
            await self._register_commands()
            delay = POLL_RETRY_SECONDS
            while True:
                try:
                    await self.poll_once()
                    delay = POLL_RETRY_SECONDS
                except TelegramError as e:
                    log.warning("Polling failed: %s", e)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, POLL_RETRY_LIMIT_SECONDS)
        finally:
            model.cancel()
            # Before the handler goes, so a turn still running can't have a change approved
            if self.busy:
                self.turn.cancel()
                await asyncio.gather(self.turn, return_exceptions=True)
            tools.confirm_handler = None

    async def _start_model(self) -> None:
        """Have the model ready for the first question; a turn starts it too if this fails."""
        try:
            await asyncio.to_thread(mlx_server.ensure_chat)
        except mlx_server.ModelServerError as e:
            log.error("Could not start the model server: %s", e)

    async def _register_commands(self) -> None:
        try:
            await telegram_api.set_commands(COMMANDS)
        except TelegramError as e:
            log.warning("Could not set the command menu: %s", e)

    async def poll_once(self) -> None:
        """Handle one batch of updates. The offset moves on first, so a crash can't repeat a turn."""
        for update in await telegram_api.get_updates(self.offset):
            self.offset = update["update_id"] + 1
            await self.handle(update)

    async def handle(self, update: dict) -> None:
        """Act on one update. A failure here is logged and doesn't affect the next update."""
        try:
            if "callback_query" in update:
                await self._on_button(update["callback_query"])
            elif "message" in update:
                await self._on_message(update["message"])
        except TelegramError as e:
            log.warning("Could not answer an update: %s", e)
        except Exception:
            log.exception("Could not handle update %s", update.get("update_id"))

    async def _on_message(self, message: dict) -> None:
        sender = message["from"]["id"]
        if sender != self.owner or message["chat"]["type"] != "private":
            log.info("Ignored a message from user %s", sender)
            return
        text = message.get("text", "").strip()
        if not text:
            await telegram_api.send_message(self.owner, TEXT_ONLY)
        elif text.startswith("/"):
            await self._on_command(text)
        elif self.busy:
            await telegram_api.send_message(self.owner, BUSY)
        else:
            self.turn = asyncio.create_task(self._run_turn(text))

    async def _on_command(self, text: str) -> None:
        name = text[1:].split(maxsplit=1)[0].partition("@")[0].lower() if text[1:].strip() else ""
        if name == "clear":
            if self.busy:
                reply = BUSY
            else:
                self.conversation.clear()
                reply = "Conversation cleared."
        elif name == "stop":
            if self.busy:
                self.turn.cancel()
                await asyncio.gather(self.turn, return_exceptions=True)
                reply = "Stopped."
            else:
                reply = "Nothing to stop."
        else:
            reply = HELP
        await telegram_api.send_message(self.owner, reply)

    async def _run_turn(self, question: str) -> None:
        typing = asyncio.create_task(self._keep_typing())
        try:
            parts = await self._reply_to(question)
        finally:
            typing.cancel()
        await self._send(parts)

    async def _reply_to(self, question: str) -> list[Part]:
        """Run the turn and return the messages to send."""
        reply = ""
        failure = None
        async with aclosing(self.conversation.stream_reply(question)) as events:
            async for event in events:
                if isinstance(event, Text):
                    reply += event.text
                elif isinstance(event, Failed):
                    failure = event.message
        return reply_parts(reply, failure)

    async def _keep_typing(self) -> None:
        """Show "typing…" for the whole turn, except while waiting for the user's answer."""
        while True:
            if self.pending is None:
                try:
                    await telegram_api.send_typing(self.owner)
                except TelegramError as e:
                    log.warning("Could not show typing: %s", e)
            await asyncio.sleep(TYPING_SECONDS)

    async def _send(self, parts: list[Part]) -> None:
        """Send a reply's parts, trying each a few times."""
        for index, (text, entities) in enumerate(parts):
            if index:
                await asyncio.sleep(PART_PAUSE_SECONDS)
            for attempt in range(SEND_ATTEMPTS):
                try:
                    await telegram_api.send_message(self.owner, text, entities=entities)
                    break
                except TelegramError as e:
                    if isinstance(e, TelegramRejected) and entities:
                        log.warning("Telegram refused a reply's formatting, sending it as plain text: %s", e)
                        entities = []
                        continue
                    log.warning("Could not send a reply (attempt %d): %s", attempt + 1, e)
                    if attempt + 1 < SEND_ATTEMPTS:
                        await asyncio.sleep(SEND_RETRY_SECONDS)

    async def confirm(self, question: str) -> bool:
        """Ask the user yes or no with buttons, for tools that change something. If the question
        can't be sent, the error fails the tool, so nothing changes."""
        pending = _Question(secrets.token_hex(4), asyncio.get_running_loop().create_future())
        sent = await telegram_api.send_message(self.owner, question, [("Yes", f"{pending.id}:yes"),
                                                                      ("No", f"{pending.id}:no")])
        self.pending = pending
        outcome = "Stopped"
        try:
            try:
                answer = await asyncio.wait_for(pending.answer, CONFIRM_SECONDS)
            except TimeoutError:
                outcome = TIMED_OUT
                return False
            outcome = "Yes" if answer else "No"
            return answer
        finally:
            self.pending = None
            await self._close_question(sent["message_id"], f"{question}\n\n{outcome}")

    async def _close_question(self, message_id: int, text: str) -> None:
        try:
            await telegram_api.edit_message_text(self.owner, message_id, text)
        except TelegramError as e:
            log.warning("Could not remove the buttons: %s", e)

    async def _on_button(self, press: dict) -> None:
        sender = press["from"]["id"]
        if sender != self.owner:
            log.info("Ignored a button press from user %s", sender)
            return
        pending = self.pending
        data = press.get("data", "")
        if pending is None or data not in (f"{pending.id}:yes", f"{pending.id}:no"):
            await telegram_api.answer_callback(press["id"], EXPIRED)
            return
        if not pending.answer.done():
            pending.answer.set_result(data.endswith(":yes"))
        await telegram_api.answer_callback(press["id"])

def setup_logging() -> None:
    """Log to the terminal and a small file. Never message text, and never httpx's INFO lines,
    which print full URLs and so the token."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if LOG_FILE is not None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

async def serve() -> None:
    """Run the bot until a signal asks it to stop, then stop the model servers it started."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    stop_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    for stop_signal in stop_signals:
        loop.add_signal_handler(stop_signal, task.cancel)
    try:
        await Bot().run()
    except asyncio.CancelledError:
        log.info("Stopping")
    finally:
        for stop_signal in stop_signals:
            loop.remove_signal_handler(stop_signal)
        # Here and not left to atexit, which runs too late to end a model start still waiting
        await asyncio.to_thread(mlx_server.stop_all)

def main() -> int:
    setup_logging()
    try:
        asyncio.run(serve())
    except SetupError as e:
        log.error("%s", e)
        return EXIT_SETUP_ERROR
    return 0

if __name__ == "__main__":
    sys.exit(main())
