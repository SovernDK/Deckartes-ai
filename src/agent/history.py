"""Saved conversations: every finished turn of the app and the Telegram bot, kept between runs.

Conversation writes here (start, add, add_summary); the app's history panel reads (list_conversations,
load) and deletes. A conversation's row is written with its first finished turn, so nothing is
left behind by a conversation that never got an answer. Dates are local time, "YYYY-MM-DD HH:MM".
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from core import clock
from core.config import config

# Longest title in the history panel; the first question, cut short
TITLE_LENGTH = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    preset TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model TEXT
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
"""

@dataclass
class Message:
    role: str
    content: str
    created_at: str
    # The model that wrote it; only for assistant messages
    model: str | None = None

@dataclass
class Listed:
    """One conversation in the history panel."""
    id: int
    title: str
    source: str
    updated_at: str

@dataclass
class Saved:
    id: int
    source: str
    preset: str
    system_prompt: str
    messages: list[Message]

def now() -> str:
    return clock.now().strftime("%Y-%m-%d %H:%M")

@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    """A connection whose changes are committed together at the end, or not at all."""
    config.history_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.history_path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()

def _insert(db: sqlite3.Connection, conversation_id: int, messages: list[Message]) -> None:
    db.executemany("INSERT INTO messages (conversation_id, role, content, created_at, model) VALUES (?, ?, ?, ?, ?)",
                   [(conversation_id, m.role, m.content, m.created_at, m.model) for m in messages])
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (messages[-1].created_at, conversation_id))

def add(conversation_id: int | None, messages: list[Message], *, source: str, preset: str,
        system_prompt: str) -> int:
    """Save a finished turn's messages and return the conversation's id. A conversation that
    isn't saved yet, or was deleted meanwhile, is started anew with these messages."""
    with _database() as db:
        exists = conversation_id is not None and db.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not exists:
            started = messages[0].created_at
            conversation_id = db.execute(
                "INSERT INTO conversations (source, preset, system_prompt, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (source, preset, system_prompt, started, started)).lastrowid
        _insert(db, conversation_id, messages)
        return conversation_id

def add_summary(conversation_id: int, summary: Message) -> None:
    """Save a compaction summary; the messages it replaced stay. Does nothing for a deleted conversation."""
    with _database() as db:
        if db.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
            _insert(db, conversation_id, [summary])

def list_conversations() -> list[Listed]:
    """Every saved conversation, the latest activity first."""
    with _database() as db:
        rows = db.execute("""
            SELECT c.id, c.source, c.updated_at,
                   (SELECT content FROM messages WHERE conversation_id = c.id AND role = 'user' ORDER BY id LIMIT 1)
            FROM conversations c ORDER BY c.updated_at DESC, c.id DESC""").fetchall()
    return [Listed(id, _title(question or ""), source, updated) for id, source, updated, question in rows]

def _title(question: str) -> str:
    line = " ".join(question.split())
    return line if len(line) <= TITLE_LENGTH else line[:TITLE_LENGTH - 1].rstrip() + "…"

def load(conversation_id: int) -> Saved:
    """A saved conversation with all its messages; KeyError when there's no such conversation."""
    with _database() as db:
        row = db.execute("SELECT source, preset, system_prompt FROM conversations WHERE id = ?",
                         (conversation_id,)).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        messages = [Message(*m) for m in db.execute(
            "SELECT role, content, created_at, model FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,))]
    return Saved(conversation_id, *row, messages)

def delete(conversation_id: int) -> None:
    with _database() as db:
        db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
