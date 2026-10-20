"""Tests for Conversation.stream_reply with a fake stream_response: what it yields and saves."""

import unittest
from contextlib import aclosing
from unittest import mock

import conversation
from conversation import Conversation
from ollama_api import Failed, Text, ToolsDone, ToolStarted

def fake_stream(*events, tokens=7):
    """A stream_response that counts tokens and yields events."""
    async def stream_response(messages, usage=None):
        if usage is not None:
            usage.input += tokens
        for event in events:
            yield event
    return stream_response

class StreamReplyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conversation = Conversation()
        patcher = mock.patch.object(conversation, "system_message", return_value="Current date: ...")
        patcher.start()
        self.addCleanup(patcher.stop)

    def stream(self, *events, **kwargs):
        patcher = mock.patch.object(conversation, "stream_response", fake_stream(*events, **kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def reply(self, query="q"):
        return [event async for event in self.conversation.stream_reply(query)]

    async def test_clean_turn_is_saved(self):
        events = [Text("Let me check."), Text("\n\n"), ToolStarted("get_cs_team"), ToolsDone(), Text("Done.")]
        self.stream(*events)
        self.assertEqual(await self.reply("When does NAVI play?"), events)
        self.assertEqual(self.conversation.history, [
            {"role": "user", "content": "When does NAVI play?"},
            {"role": "assistant", "content": "Let me check.\n\nDone."},
        ])
        self.assertEqual(self.conversation.last_usage.input, 7)

    async def test_failed_turn_is_not_saved_but_counted(self):
        self.stream(Text("partial"), Failed("connection lost"))
        self.assertEqual(await self.reply(), [Text("partial"), Failed("connection lost")])
        self.assertEqual(self.conversation.history, [])
        self.assertEqual(self.conversation.total_usage.input, 7)

    async def test_error_looking_reply_is_saved(self):
        self.stream(Text("**Error:** is how markdown bolds a label"))
        await self.reply()
        self.assertEqual(self.conversation.history[-1]["content"], "**Error:** is how markdown bolds a label")

    async def test_empty_reply_is_not_saved(self):
        self.stream(ToolStarted("a"), ToolsDone())
        await self.reply()
        self.assertEqual(self.conversation.history, [])

    async def test_closed_turn_is_not_saved_but_counted(self):
        self.stream(Text("one"), Text("two"))
        async with aclosing(self.conversation.stream_reply("q")) as events:
            async for event in events:
                break
        self.assertEqual(self.conversation.history, [])
        self.assertEqual(self.conversation.total_usage.input, 7)

    async def test_history_is_sent_with_the_next_question(self):
        self.stream(Text("first"))
        await self.reply("one")
        sent = []
        async def stream_response(messages, usage=None):
            sent.extend(messages)
            yield Text("second")
        with mock.patch.object(conversation, "stream_response", stream_response):
            await self.reply("two")
        self.assertEqual([m["content"] for m in sent], ["Current date: ...", "one", "first", "two"])

    async def test_system_prompt_error(self):
        self.stream(Text("never"))
        with mock.patch.object(conversation, "system_message", side_effect=ValueError("bad json")):
            self.assertEqual(await self.reply(), [Failed("could not load the system prompt: bad json")])
        self.assertEqual(self.conversation.history, [])

    async def test_file_search_error(self):
        self.stream(Text("never"))
        self.conversation.document = mock.Mock(whole=False)
        with mock.patch.object(conversation.rag, "search", side_effect=OSError("cache gone")):
            self.assertEqual(await self.reply(), [Failed("could not search the loaded file: cache gone")])

if __name__ == "__main__":
    unittest.main()
