"""The Telegram Bot API calls telegram_bot.py makes, one function each.

The token is part of every URL, so errors raised here have it removed and carry no cause: their
text reaches the log and, through a failed tool, the model.
"""

from core import http_client
from core.config import config

# How long one getUpdates call waits for something to happen
POLL_SECONDS = 50
# Under Telegram's limit of 4096
MAX_MESSAGE = 4000

class TelegramError(Exception):
    """A Bot API call failed. The message says why and never contains the token."""

def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Cut text into parts of at most limit characters, at a paragraph, line or word if one is
    in the second half of the part."""
    parts = []
    while len(text) > limit:
        window = text[:limit + 1]
        cut = next((at for at in map(window.rfind, ("\n\n", "\n", " ")) if at >= limit // 2), limit)
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return [part for part in parts if part.strip()]

_STATUS_MESSAGES = {
    401: "Telegram refused the bot token: check TELEGRAM_BOT_TOKEN",
    # Every method called here exists, so a 404 is a token Telegram can't parse
    404: "Telegram can't read the bot token: TELEGRAM_BOT_TOKEN should be digits, a colon and letters, nothing else",
    409: "Another copy of the bot is already polling Telegram",
}

async def _call(method: str, payload: dict, timeout: float = 30):
    token = config.telegram_token
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = await http_client.request("POST", url, name="Telegram", error=TelegramError, json=payload,
                                             timeout=timeout, status_messages=_STATUS_MESSAGES)
        return response.json()["result"]
    except TelegramError as e:
        raise TelegramError(str(e).replace(token, "<token>")) from None
    except (ValueError, KeyError):
        raise TelegramError(f"Telegram sent an unreadable answer to {method}") from None

async def get_updates(offset: int | None) -> list[dict]:
    """Wait up to POLL_SECONDS for updates after those already confirmed by offset."""
    payload = {"timeout": POLL_SECONDS, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return await _call("getUpdates", payload, timeout=POLL_SECONDS + 10)

async def send_message(chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> dict:
    """Send plain text, with an optional row of (label, callback data) buttons."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data}
                                                        for label, data in buttons]]}
    return await _call("sendMessage", payload)

async def send_typing(chat_id: int) -> None:
    await _call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

async def answer_callback(callback_id: str, text: str | None = None) -> None:
    """Acknowledge a button press, which otherwise keeps spinning on the phone."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    await _call("answerCallbackQuery", payload)

async def edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    """Replace a message's text, which also removes its buttons."""
    await _call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})

async def set_commands(commands: dict[str, str]) -> None:
    """Set the command menu shown in the Telegram app."""
    await _call("setMyCommands", {"commands": [{"command": name, "description": description}
                                               for name, description in commands.items()]})
