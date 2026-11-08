"""Tests for Conversation.stream_reply with a fake stream_response: what it yields and saves."""

import unittest
from contextlib import aclosing
from unittest import mock

import conversation
from conversation import Conversation
from ollama_api import Failed, Text, ToolsDone, ToolStarted, TurnInfo, Usage

def fake_stream(*events, tokens=7):
    """A stream_response that counts tokens and yields events."""
    async def stream_response(messages, turn=None):
        if turn is not None:
            turn.usage.input += tokens
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

    def capture_sent(self) -> list:
        """A stream_response that answers "ok"; returns the list its messages are collected in."""
        sent = []
        async def stream_response(messages, turn=None):
            sent.extend(messages)
            yield Text("ok")
        patcher = mock.patch.object(conversation, "stream_response", stream_response)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent

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
        self.assertEqual(self.conversation.last_turn.usage.input, 7)

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
        async def stream_response(messages, turn=None):
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

    async def test_each_turn_has_its_own_info_and_tokens_add_up(self):
        self.stream(Text("ok"))
        await self.reply()
        first = self.conversation.last_turn
        await self.reply()
        self.assertIsNot(self.conversation.last_turn, first)
        self.assertEqual(self.conversation.total_usage.total, 14)

    async def test_clear_forgets_the_turn_and_the_total(self):
        self.stream(Text("ok"))
        await self.reply()
        self.conversation.document = mock.Mock(whole=True)
        self.conversation.clear()
        self.assertIsNone(self.conversation.last_turn.summary())
        self.assertEqual(self.conversation.total_usage.total, 0)
        self.assertEqual(self.conversation.history, [])
        self.assertIsNone(self.conversation.document)

    async def test_excerpts_go_just_before_the_question(self):
        sent = self.capture_sent()
        self.conversation.document = mock.Mock(whole=False)
        with mock.patch.object(conversation.rag, "search", return_value=["a", "b"]), \
             mock.patch.object(conversation.rag, "excerpts_prompt", return_value="excerpts"):
            await self.reply()
        self.assertEqual([m["content"] for m in sent], ["Current date: ...", "excerpts", "q"])
        self.assertEqual(self.conversation.last_turn.file_context, "2 excerpts")

    async def test_whole_file_goes_in_the_system_message(self):
        sent = self.capture_sent()
        self.conversation.document = mock.Mock(whole=True)
        with mock.patch.object(conversation.rag, "whole_file_prompt", return_value="file"):
            await self.reply()
        self.assertEqual([m["content"] for m in sent], ["Current date: ...\n\nfile", "q"])
        self.assertEqual(self.conversation.last_turn.file_context, "whole")

class SummaryTest(unittest.TestCase):
    def test_nothing_sent_to_the_model(self):
        self.assertIsNone(TurnInfo().summary())

    def test_tokens_only(self):
        self.assertEqual(TurnInfo(Usage(input=1200, output=34)).summary(), "1,200 in · 34 out · 1,234 tokens")

    def test_one_token_is_singular(self):
        self.assertEqual(TurnInfo(Usage(input=1)).summary(), "1 in · 0 out · 1 token")

    def test_generation_speed(self):
        self.assertEqual(TurnInfo(Usage(input=1, output=5, output_ns=2_000_000_000)).summary(),
                         "1 in · 5 out · 6 tokens · 2.5 tok/s")

    def test_tool_groups(self):
        self.assertEqual(TurnInfo(Usage(input=1), tool_groups=["cs", "anime"]).summary(),
                         "1 in · 0 out · 1 token · tools: cs, anime")

    def test_router_chose_no_tools(self):
        self.assertEqual(TurnInfo(Usage(input=1), tool_groups=[]).summary(), "1 in · 0 out · 1 token · tools: none")

    def test_loaded_file(self):
        self.assertEqual(TurnInfo(Usage(input=1), file_context="5 excerpts").summary(),
                         "1 in · 0 out · 1 token · file: 5 excerpts")

if __name__ == "__main__":
    unittest.main()
