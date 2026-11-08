"""A turn from start to finish: Conversation.stream_reply with only the Ollama client faked."""

import itertools
import unittest
from contextlib import aclosing
from types import SimpleNamespace as NS
from unittest import mock

import ollama_client
import prompts
import rag
import tool_router
from config import config
from conversation import MAX_TOOL_ROUNDS, Conversation, Failed, Text, ToolsDone, ToolStarted, TurnInfo, Usage
from frozen_clock import freeze
from test_tool_router import FakeEmbeddings, group_tools, names

DATE_LINE = "Current date: Wednesday 2026-11-11, local time 12:00 (Europe/Warsaw)."
SEASON_TEXT = "Today is Wednesday 2026-11-11. Current anime season: Fall 2026."
_models = itertools.count()

def chunk(content="", tool_calls=None, done=False):
    return NS(message=NS(content=content, tool_calls=tool_calls), done=done,
              prompt_eval_count=10 if done else None, eval_count=5 if done else None,
              eval_duration=10**9 if done else None)

def call(tool, **arguments):
    return NS(function=NS(name=tool, arguments=arguments))

class FakeClient:
    """Answers each chat call with the next round's chunks (an exception among them is raised
    there), and records what each call sent."""

    def __init__(self, *rounds, capabilities=("tools",)):
        self.rounds = list(rounds)
        self.capabilities = capabilities
        self.sent = []
        self.shown = 0
        self.closed = 0

    async def show(self, model):
        self.shown += 1
        return NS(capabilities=list(self.capabilities))

    async def chat(self, **kwargs):
        self.sent.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.rounds:
            raise ConnectionError("no more rounds")
        chunks = self.rounds.pop(0)
        async def stream():
            try:
                for c in chunks:
                    if isinstance(c, Exception):
                        raise c
                    yield c
            finally:
                self.closed += 1
        return stream()

class TurnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        freeze(self, "2026-11-11 12:00")
        self.patch(config, "system_prompt", "No such preset")
        self.patch(config, "tool_routing", False)
        # Whether a model supports tools is remembered per model, so each test has its own
        self.patch(config, "model", f"model-{next(_models)}")
        self.conversation = Conversation()

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, *rounds, **kwargs) -> FakeClient:
        client = FakeClient(*rounds, **kwargs)
        patcher = mock.patch.dict(ollama_client._clients, {("async", config.host): client})
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def route(self, scores: dict[str, dict]) -> None:
        self.patch(config, "tool_routing", True)
        self.patch(tool_router, "_embed", FakeEmbeddings(scores))
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)

    async def reply(self, query="q"):
        return [event async for event in self.conversation.stream_reply(query)]

    def sent_contents(self, client, index=0):
        return [message["content"] for message in client.sent[index]["messages"]]

class FinishedTurnTest(TurnTest):
    async def test_reply_is_streamed_saved_and_counted(self):
        self.use([chunk("Hel"), chunk("lo", done=True)])
        self.assertEqual(await self.reply("hi"), [Text("Hel"), Text("lo")])
        self.assertEqual(self.conversation.history, [{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "Hello"}])
        usage = self.conversation.last_turn.usage
        self.assertEqual((usage.input, usage.output), (10, 5))
        self.assertEqual(self.conversation.total_usage.total, 15)

    async def test_system_message_comes_first(self):
        client = self.use([chunk("ok", done=True)])
        await self.reply("hi")
        self.assertEqual(client.sent[0]["messages"], [{"role": "system", "content": DATE_LINE},
                                                      {"role": "user", "content": "hi"}])

    async def test_history_is_sent_with_the_next_question(self):
        client = self.use([chunk("first", done=True)], [chunk("second", done=True)])
        await self.reply("one")
        await self.reply("two")
        self.assertEqual(self.sent_contents(client, 1), [DATE_LINE, "one", "first", "two"])

    async def test_empty_reply_is_not_saved(self):
        self.use([chunk(done=True)])
        self.assertEqual(await self.reply(), [])
        self.assertEqual(self.conversation.history, [])

    async def test_error_looking_reply_is_an_ordinary_reply(self):
        text = "**Error:** is how markdown bolds a label"
        self.use([chunk(text, done=True)])
        self.assertEqual(await self.reply(), [Text(text)])
        self.assertEqual(self.conversation.history[-1]["content"], text)

    async def test_each_turn_has_its_own_info_and_tokens_add_up(self):
        self.use([chunk("a", done=True)], [chunk("b", done=True)])
        await self.reply()
        first = self.conversation.last_turn
        await self.reply()
        self.assertIsNot(self.conversation.last_turn, first)
        self.assertEqual(self.conversation.total_usage.total, 30)

    async def test_clear_forgets_everything(self):
        self.use([chunk("ok", done=True)])
        await self.reply()
        self.conversation.document = mock.Mock(whole=True)
        self.conversation.clear()
        self.assertIsNone(self.conversation.last_turn.summary())
        self.assertEqual(self.conversation.total_usage.total, 0)
        self.assertEqual(self.conversation.history, [])
        self.assertIsNone(self.conversation.document)

class ToolRoundTest(TurnTest):
    async def test_tool_result_goes_back_to_the_model(self):
        client = self.use([chunk("Let me check."), chunk(tool_calls=[call("get_current_season")], done=True)],
                          [chunk("It is fall.", done=True)])
        self.assertEqual(await self.reply(), [Text("Let me check."), Text("\n\n"), ToolStarted("get_current_season"),
                                              ToolsDone(), Text("It is fall.")])
        self.assertEqual(client.sent[1]["messages"][-2:], [
            {"role": "assistant", "content": "Let me check.", "tool_calls": [call("get_current_season")]},
            {"role": "tool", "tool_name": "get_current_season", "content": SEASON_TEXT},
        ])
        self.assertEqual(self.conversation.history[-1]["content"], "Let me check.\n\nIt is fall.")
        self.assertEqual(self.conversation.last_turn.usage.input, 20)

    async def test_tool_messages_are_not_kept_between_turns(self):
        client = self.use([chunk(tool_calls=[call("get_current_season")], done=True)], [chunk("fall", done=True)],
                          [chunk("ok", done=True)])
        await self.reply("one")
        await self.reply("two")
        self.assertEqual(self.sent_contents(client, 2), [DATE_LINE, "one", "fall", "two"])

    async def test_round_without_text_has_no_break(self):
        self.use([chunk(tool_calls=[call("a"), call("b")], done=True)], [chunk("ok", done=True)])
        self.assertEqual(await self.reply(), [ToolStarted("a"), ToolStarted("b"), ToolsDone(), Text("ok")])

    async def test_too_many_rounds_fails_and_is_not_saved(self):
        self.use(*([chunk(tool_calls=[call("a")], done=True)] for _ in range(MAX_TOOL_ROUNDS)))
        events = await self.reply()
        self.assertEqual(events[-1], Failed(f"model still calling tools after {MAX_TOOL_ROUNDS} rounds"))
        self.assertEqual(events.count(ToolsDone()), MAX_TOOL_ROUNDS)
        self.assertEqual(self.conversation.history, [])

class ModelCallTest(TurnTest):
    async def test_context_length_and_temperature_are_sent(self):
        client = self.use([chunk("ok", done=True)])
        with mock.patch.object(config, "context_length", 8192), mock.patch.object(config, "temperature", 0.2):
            await self.reply()
        self.assertEqual(client.sent[0]["options"], {"num_ctx": 8192, "temperature": 0.2})

    async def test_temperature_left_to_the_model(self):
        client = self.use([chunk("ok", done=True)])
        with mock.patch.object(config, "context_length", 8192), mock.patch.object(config, "temperature", None):
            await self.reply()
        self.assertEqual(client.sent[0]["options"], {"num_ctx": 8192})

    async def test_model_without_tool_support_gets_none(self):
        client = self.use([chunk("ok", done=True)], capabilities=())
        await self.reply()
        self.assertIsNone(client.sent[0]["tools"])
        self.assertIsNone(self.conversation.last_turn.tool_groups)

    async def test_tool_support_is_checked_once_per_model(self):
        client = self.use([chunk("a", done=True)], [chunk("b", done=True)])
        await self.reply()
        await self.reply()
        self.assertEqual(client.shown, 1)

class RoutingTest(TurnTest):
    async def test_chosen_tools_are_sent_and_recorded(self):
        client = self.use([chunk("ok", done=True)])
        self.route({"Is NAVI playing?": {"cs": 0.9}})
        await self.reply("Is NAVI playing?")
        self.assertEqual(names(client.sent[0]["tools"]), group_tools("cs"))
        self.assertEqual(self.conversation.last_turn.tool_groups, ["cs"])

    async def test_follow_up_borrows_the_previous_question(self):
        client = self.use([chunk("a", done=True)], [chunk("b", done=True)])
        self.route({"Which anime air today?": {"anime": 0.9}, "and tomorrow?": {"calendar": 0.5}})
        await self.reply("Which anime air today?")
        await self.reply("and tomorrow?")
        self.assertEqual(names(client.sent[1]["tools"]), group_tools("anime"))
        self.assertEqual(self.conversation.last_turn.tool_groups, ["anime"])

class FailedTurnTest(TurnTest):
    async def test_failure_is_last_and_turn_is_not_saved_but_counted(self):
        self.use([chunk("partial", done=True), ConnectionError("connection lost")])
        self.assertEqual(await self.reply(), [Text("partial"), Failed("connection lost")])
        self.assertEqual(self.conversation.history, [])
        self.assertEqual(self.conversation.total_usage.input, 10)

    async def test_failure_before_any_text(self):
        self.use()
        self.assertEqual(await self.reply(), [Failed("no more rounds")])

    async def test_system_prompt_error(self):
        client = self.use([chunk("never", done=True)])
        self.patch(prompts, "load_prompts", mock.Mock(side_effect=ValueError("bad json")))
        self.assertEqual(await self.reply(), [Failed("could not load the system prompt: bad json")])
        self.assertEqual(client.sent, [])
        self.assertEqual(self.conversation.history, [])

    async def test_file_search_error(self):
        client = self.use([chunk("never", done=True)])
        self.conversation.document = mock.Mock(whole=False)
        self.patch(rag, "search", mock.Mock(side_effect=OSError("cache gone")))
        self.assertEqual(await self.reply(), [Failed("could not search the loaded file: cache gone")])
        self.assertEqual(client.sent, [])

    async def test_closed_turn_is_not_saved_but_counted(self):
        self.use([chunk("one", done=True), chunk("two")])
        async with aclosing(self.conversation.stream_reply("q")) as events:
            async for event in events:
                if event == Text("two"):
                    break
        self.assertEqual(self.conversation.history, [])
        self.assertEqual(self.conversation.total_usage.input, 10)

    async def test_closing_a_turn_closes_the_model_stream_at_once(self):
        client = self.use([chunk("one"), chunk("two")])
        async with aclosing(self.conversation.stream_reply("q")) as events:
            async for event in events:
                break
        self.assertEqual(client.closed, 1)

class LoadedFileTest(TurnTest):
    async def test_excerpts_go_just_before_the_question(self):
        client = self.use([chunk("ok", done=True)])
        self.conversation.document = mock.Mock(whole=False)
        self.patch(rag, "search", mock.Mock(return_value=["a", "b"]))
        self.patch(rag, "excerpts_prompt", mock.Mock(return_value="excerpts"))
        await self.reply()
        self.assertEqual(self.sent_contents(client), [DATE_LINE, "excerpts", "q"])
        self.assertEqual(self.conversation.last_turn.file_context, "2 excerpts")

    async def test_whole_file_goes_in_the_system_message(self):
        client = self.use([chunk("ok", done=True)])
        self.conversation.document = mock.Mock(whole=True)
        self.patch(rag, "whole_file_prompt", mock.Mock(return_value="file"))
        await self.reply()
        self.assertEqual(self.sent_contents(client), [f"{DATE_LINE}\n\nfile", "q"])
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
