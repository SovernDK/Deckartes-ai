"""Conversation with the AI model: history and turns, independent of the UI.

A turn is added to the history only when the model finishes it without error. A turn
that fails or is interrupted (its stream closed or task cancelled) leaves the history as it was.
Tokens are counted for every turn, including failed ones, since the model still used them.

A loaded file (rag.py) goes into every turn: a short file whole, at the end of the system message,
where it stays the same each turn so Ollama can reuse its cache; a long one as excerpts picked
for the question, just before it. Neither is saved in the history.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

from ollama_api import Event, Failed, Text, Usage, stream_response
from prompts import system_message
import rag

class Conversation:
    def __init__(self) -> None:
        # Finished exchanges in the Ollama messages format
        self.history: list[dict] = []
        # Tokens of the latest turn and of the whole conversation
        self.last_usage = Usage()
        self.total_usage = Usage()
        self.document: rag.Document | None = None

    def clear(self) -> None:
        self.history.clear()
        self.last_usage = Usage()
        self.total_usage = Usage()
        self.document = None

    def messages_for(self, query: str, excerpts: str | None = None) -> list[dict]:
        """What gets sent to the model for a new question: the current system prompt
        (read fresh, so a changed preset applies from the next turn), the history and the question,
        with the loaded file: whole in the system message, or excerpts just before the question."""
        system = system_message()
        if self.document is not None and self.document.whole:
            system = f"{system}\n\n{rag.whole_file_prompt(self.document)}"
        messages = [{"role": "system", "content": system}] if system else []
        before = [{"role": "system", "content": excerpts}] if excerpts else []
        return [*messages, *self.history, *before, {"role": "user", "content": query}]

    async def file_excerpts(self, query: str, usage: Usage) -> str | None:
        """Excerpts of a long loaded file for this question; None without one or for a short file."""
        document = self.document
        if document is None:
            return None
        if document.whole:
            usage.file_context = "whole"
            return None
        questions = [m["content"] for m in self.history if m["role"] == "user"]
        chunks = await asyncio.to_thread(rag.search, document, query, questions[-1] if questions else None)
        usage.file_context = f"{len(chunks)} excerpts"
        return rag.excerpts_prompt(document, chunks)

    def remember(self, query: str, reply: str) -> None:
        if reply:
            self.history.append({"role": "user", "content": query})
            self.history.append({"role": "assistant", "content": reply})

    async def stream_reply(self, query: str) -> AsyncIterator[Event]:
        """Yield the turn's events (see ollama_api). A Failed comes last and the turn isn't saved."""
        self.last_usage = usage = Usage()
        try:
            excerpts = await self.file_excerpts(query, usage)
        except Exception as e:
            yield Failed(f"could not search the loaded file: {e}")
            return
        try:
            messages = self.messages_for(query, excerpts)
        except Exception as e:
            # e.g. a broken prompts.json
            yield Failed(f"could not load the system prompt: {e}")
            return
        reply = ""
        try:
            # aclosing: closing this generator also closes the model stream right away
            async with aclosing(stream_response(messages, usage)) as events:
                async for event in events:
                    yield event
                    if isinstance(event, Failed):
                        return
                    if isinstance(event, Text):
                        reply += event.text
        finally:
            self.total_usage.add(usage)
        self.remember(query, reply)
