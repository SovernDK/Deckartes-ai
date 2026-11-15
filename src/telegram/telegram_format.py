"""Turn the model's Markdown reply into Telegram messages: plain text plus formatting entities.

Entities need no parse_mode, so nothing in the reply can make Telegram fail to parse it.
"""

from telegramify_markdown import MessageEntity, convert, split_entities, utf16_len

from telegram.telegram_api import MAX_MESSAGE

EMPTY_REPLY = "The model returned an empty reply."

# One message: its text and its entities as Bot API dicts
Part = tuple[str, list[dict]]


def reply_parts(reply: str, failure: str | None = None) -> list[Part]:
    """The messages for a turn's reply, each under Telegram's limit, with the error last if it failed."""
    text, entities = convert(reply)
    if not text.strip():
        text, entities = "", []
    if failure:
        text += "\n\n" if text else ""
        # Added after converting, so the message is never read as Markdown
        entities.append(MessageEntity("bold", utf16_len(text), len("Error:")))
        text += f"Error: {failure}"
    if not text:
        return [(EMPTY_REPLY, [])]
    return [
        (part, [entity.to_dict() for entity in part_entities])
        for part, part_entities in split_entities(text, entities, MAX_MESSAGE)
    ]
