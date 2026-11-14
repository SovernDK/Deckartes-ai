"""A turn from start to finish: Conversation.stream_reply with only the model client faked."""

import unittest
from contextlib import aclosing
from unittest import mock

from agent import prompts, tool_router
from agent.conversation import (
    MAX_TOOL_ROUNDS,
    Compacted,
    Compacting,
    Conversation,
    Failed,
    Text,
    Thinking,
    ToolsDone,
    ToolStarted,
    TurnInfo,
    Usage,
)
from core.config import config
from frozen_clock import freeze
from model import mlx_client
from test_tool_router import FakeEmbeddings, group_tools, names

DATE_LINE = "Current date: Wednesday 2026-11-11 (Europe/Warsaw)."
TIME_LINE = "Current local time: 12:00."
TIME = {"role": "system", "content": TIME_LINE}
SEASON_TEXT = "Today is Wednesday 2026-11-11. Current anime season: Fall 2026."

def chunk(content="", tool_calls=None, done=False):
    return mlx_client.Delta(content, tool_calls or [], mlx_client.TokenCount(10, 5, 10**9) if done else None)

def think(text):
    return mlx_client.Delta(reasoning=text)

def call(tool, **arguments):
    return mlx_client.ToolCall(f"call_{tool}", tool, arguments)

class FakeClient:
    """Stands in for mlx_client's chat calls: answers each with the next round's chunks (an
    exception among them is raised there), and records what each call sent."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.sent = []
        self.closed = 0

    def next_round(self, messages, tools):
        self.sent.append({"messages": list(messages), "tools": tools})
        if not self.rounds:
            raise ConnectionError("no more rounds")
        return self.rounds.pop(0)

    async def complete(self, messages):
        chunks = self.next_round(messages, None)
        if isinstance(chunks[0], Exception):
            raise chunks[0]
        return mlx_client.Reply("".join(c.content for c in chunks), mlx_client.TokenCount(10, 5, 10**9))

    async def stream_chat(self, messages, tools=None):
        chunks = self.next_round(messages, tools)
        try:
            for chunk in chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk
        finally:
            self.closed += 1

class TurnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        freeze(self, "2026-11-11 12:00")
        self.patch(config, "system_prompt", "No such preset")
        self.patch(config, "tool_routing", False)
        # Tests with a tiny context window would otherwise compact on every turn
        self.patch(config, "compact_at", None)
        self.conversation = Conversation()

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, *rounds) -> FakeClient:
        client = FakeClient(*rounds)
        self.patch(mlx_client, "stream_chat", client.stream_chat)
        self.patch(mlx_client, "complete", client.complete)
        return client

    def route(self, scores: dict[str, dict]) -> None:
        self.patch(config, "tool_routing", True)
        self.patch(mlx_client, "embed", FakeEmbeddings(scores))
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)

    async def reply(self, query="q"):
        return [event async for event in self.conversation.stream_reply(query)]

    def sent_contents(self, client, index=0):
        return [message["content"] for message in client.sent[index]["messages"]]

class ReplacedToolFunctionsTest(TurnTest):
    async def test_a_replaced_tool_runner_answers_instead_of_the_real_tool_with_the_turns_confirm(self):
        calls = []
        async def runner(name, arguments, confirm):
            calls.append((name, arguments, confirm))
            return "fake result"
        async def confirm(question):
            return True
        client = self.use([chunk(tool_calls=[call("get_cs_team", name="NAVI")], done=True)], [chunk("done", done=True)])
        self.conversation = Conversation(tool_runner=runner)
        [event async for event in self.conversation.stream_reply("q", confirm)]
        self.assertEqual(calls, [("get_cs_team", {"name": "NAVI"}, confirm)])
        self.assertEqual(client.sent[1]["messages"][-1]["content"], "fake result")

    async def test_a_turn_that_cannot_ask_the_user_refuses_a_tool_that_changes_something(self):
        add = call("add_calendar_event", title="Dentist", date="2026-11-12", start_time="14:30")
        client = self.use([chunk(tool_calls=[add], done=True)], [chunk("sorry", done=True)])
        await self.reply()
        self.assertEqual(client.sent[1]["messages"][-1]["content"],
                         "Tool add_calendar_event needs the user's confirmation, which can't be asked for here, "
                         "so nothing was changed")

    async def test_a_replaced_tool_selector_decides_what_is_sent(self):
        async def selector(question, previous):
            self.assertEqual((question, previous), ("q", None))
            return tool_router.Selection(None, ["cs"])
        client = self.use([chunk("hi", done=True)])
        self.conversation = Conversation(tool_selector=selector)
        await self.reply()
        self.assertIsNone(client.sent[0]["tools"])
        self.assertEqual(self.conversation.last_turn.tool_groups, ["cs"])

class ThinkingTest(TurnTest):
    async def test_thinking_is_streamed_but_never_saved_or_sent_back(self):
        client = self.use([think("Need the team."), chunk(tool_calls=[call("get_cs_team", name="NAVI")], done=True)],
                          [think("Got it."), chunk("NAVI is fine.", done=True)])
        self.conversation = Conversation(tool_runner=mock.AsyncMock(return_value="result"))
        events = await self.reply("hi")
        self.assertEqual(events, [Thinking("Need the team."), ToolStarted("get_cs_team"), ToolsDone(),
                                  Thinking("Got it."), Text("NAVI is fine.")])
        self.assertEqual(self.conversation.history[-1], {"role": "assistant", "content": "NAVI is fine."})
        self.assertNotIn("Need the team.", str(client.sent[1]["messages"]))
        started = events[1]
        assert isinstance(started, ToolStarted)
        self.assertEqual(started.arguments, {"name": "NAVI"})

    async def test_thinking_without_an_answer_fails_and_is_not_saved(self):
        self.use([think("Hmm..."), chunk(" ", done=True)])
        events = await self.reply("hi")
        self.assertEqual(events, [Thinking("Hmm..."), Text(" "), Failed("the model stopped before answering")])
        self.assertEqual(self.conversation.history, [])
        self.assertEqual(self.conversation.total_usage.total, 15)

class FinishedTurnTest(TurnTest):
    async def test_reply_is_streamed_saved_and_counted(self):
        self.use([chunk("Hel"), chunk("lo", done=True)])
        self.assertEqual(await self.reply("hi"), [Text("Hel"), Text("lo")])
        self.assertEqual(self.conversation.history, [TIME, {"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "Hello"}])
        usage = self.conversation.last_turn.usage
        self.assertEqual((usage.input, usage.output), (10, 5))
        self.assertEqual(self.conversation.total_usage.total, 15)

    async def test_system_message_comes_first(self):
        client = self.use([chunk("ok", done=True)])
        await self.reply("hi")
        self.assertEqual(client.sent[0]["messages"], [{"role": "system", "content": DATE_LINE}, TIME,
                                                      {"role": "user", "content": "hi"}])

    async def test_history_is_sent_with_the_next_question(self):
        client = self.use([chunk("first", done=True)], [chunk("second", done=True)])
        await self.reply("one")
        await self.reply("two")
        self.assertEqual(self.sent_contents(client, 1), [DATE_LINE, TIME_LINE, "one", "first", TIME_LINE, "two"])

    async def test_a_new_minute_leaves_the_earlier_prompt_unchanged(self):
        # The server's prompt cache only reuses an exact start of the prompt
        client = self.use([chunk("first", done=True)], [chunk("second", done=True)])
        await self.reply("one")
        freeze(self, "2026-11-11 12:07")
        await self.reply("two")
        first, second = client.sent[0]["messages"], client.sent[1]["messages"]
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(second[-2]["content"], "Current local time: 12:07.")

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
        self.conversation.clear()
        self.assertIsNone(self.conversation.last_turn.summary())
        self.assertEqual(self.conversation.total_usage.total, 0)
        self.assertEqual(self.conversation.history, [])

class CompactTest(TurnTest):
    def setUp(self):
        super().setUp()
        self.patch(config, "context_length", 1000)
        self.patch(config, "compact_at", 0.01)  # a finished turn (15 tokens) is over it

    async def test_big_context_is_summarised_before_the_next_question(self):
        client = self.use([chunk("first", done=True)], [chunk("the gist")], [chunk("second", done=True)])
        await self.reply("one")
        self.assertEqual(await self.reply("two"), [Compacting(), Compacted(), Text("second")])
        summary = "Summary of the conversation so far:\nthe gist"
        self.assertEqual(self.sent_contents(client, 2), [DATE_LINE, summary, TIME_LINE, "two"])
        self.assertEqual(client.sent[1]["messages"][1]["content"],
                         f"system: {TIME_LINE}\n\nuser: one\n\nassistant: first")
        self.assertEqual(self.conversation.history, [{"role": "system", "content": summary}, TIME,
                                                     {"role": "user", "content": "two"},
                                                     {"role": "assistant", "content": "second"}])

    async def test_small_context_is_left_alone(self):
        self.patch(config, "compact_at", 0.5)
        client = self.use([chunk("first", done=True)], [chunk("second", done=True)])
        await self.reply("one")
        self.assertEqual(await self.reply("two"), [Text("second")])
        self.assertEqual(self.sent_contents(client, 1), [DATE_LINE, TIME_LINE, "one", "first", TIME_LINE, "two"])

    async def test_compact_at_none_never_compacts(self):
        self.patch(config, "compact_at", None)
        self.use([chunk("first", done=True)], [chunk("second", done=True)])
        await self.reply("one")
        self.assertEqual(await self.reply("two"), [Text("second")])

    async def test_failed_summary_fails_the_turn_and_keeps_the_history(self):
        self.use([chunk("first", done=True)], [ConnectionError("down")])
        await self.reply("one")
        events = await self.reply("two")
        self.assertEqual(events, [Compacting(), Failed("could not compact the conversation: down")])
        self.assertEqual([m["content"] for m in self.conversation.history], [TIME_LINE, "one", "first"])

    async def test_clear_forgets_the_context_size(self):
        self.use([chunk("first", done=True)])
        await self.reply("one")
        self.conversation.clear()
        self.assertFalse(self.conversation.needs_compacting())

class ToolRoundTest(TurnTest):
    async def test_tool_result_goes_back_to_the_model(self):
        client = self.use([chunk("Let me check."), chunk(tool_calls=[call("get_current_season")], done=True)],
                          [chunk("It is fall.", done=True)])
        self.assertEqual(await self.reply(), [Text("Let me check."), Text("\n\n"), ToolStarted("get_current_season"),
                                              ToolsDone(), Text("It is fall.")])
        self.assertEqual(client.sent[1]["messages"][-2:], [
            {"role": "assistant", "content": "Let me check.",
             "tool_calls": [{"id": "call_get_current_season", "type": "function",
                             "function": {"name": "get_current_season", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_get_current_season", "content": SEASON_TEXT},
        ])
        self.assertEqual(self.conversation.history[-1]["content"], "Let me check.\n\nIt is fall.")
        self.assertEqual(self.conversation.last_turn.usage.input, 20)

    async def test_tool_messages_are_not_kept_between_turns(self):
        client = self.use([chunk(tool_calls=[call("get_current_season")], done=True)], [chunk("fall", done=True)],
                          [chunk("ok", done=True)])
        await self.reply("one")
        await self.reply("two")
        self.assertEqual(self.sent_contents(client, 2), [DATE_LINE, TIME_LINE, "one", "fall", TIME_LINE, "two"])

    async def test_arguments_that_are_not_json_go_back_as_a_tool_failure(self):
        bad = mlx_client.ToolCall("call_1", "get_cs_team", None)
        client = self.use([chunk(tool_calls=[bad], done=True)], [chunk("sorry", done=True)])
        await self.reply()
        self.assertEqual(client.sent[1]["messages"][-1], {
            "role": "tool", "tool_call_id": "call_1",
            "content": "Tool get_cs_team failed: its arguments were not valid JSON"})

    async def test_round_without_text_has_no_break(self):
        self.use([chunk(tool_calls=[call("a"), call("b")], done=True)], [chunk("ok", done=True)])
        self.assertEqual(await self.reply(), [ToolStarted("a"), ToolStarted("b"), ToolsDone(), Text("ok")])

    async def test_too_many_rounds_fails_and_is_not_saved(self):
        self.use(*([chunk(tool_calls=[call("a")], done=True)] for _ in range(MAX_TOOL_ROUNDS)))
        events = await self.reply()
        self.assertEqual(events[-1], Failed(f"model still calling tools after {MAX_TOOL_ROUNDS} rounds"))
        self.assertEqual(events.count(ToolsDone()), MAX_TOOL_ROUNDS)
        self.assertEqual(self.conversation.history, [])

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

    async def test_failure_before_any_text_is_the_only_event(self):
        self.use()
        self.assertEqual(await self.reply(), [Failed("no more rounds")])

    async def test_unreadable_prompts_fail_the_turn_before_the_model_is_called(self):
        client = self.use([chunk("never", done=True)])
        self.patch(prompts, "load_prompts", mock.Mock(side_effect=ValueError("bad json")))
        self.assertEqual(await self.reply(), [Failed("could not load the system prompt: bad json")])
        self.assertEqual(client.sent, [])
        self.assertEqual(self.conversation.history, [])

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

class SummaryTest(unittest.TestCase):
    def test_no_summary_when_nothing_was_sent(self):
        self.assertIsNone(TurnInfo().summary())

    def test_summary_counts_tokens_in_and_out(self):
        self.assertEqual(TurnInfo(Usage(input=1200, output=34)).summary(), "1,200 in · 34 out · 1,234 tokens")

    def test_one_token_is_singular(self):
        self.assertEqual(TurnInfo(Usage(input=1)).summary(), "1 in · 0 out · 1 token")

    def test_summary_adds_the_generation_speed_when_known(self):
        self.assertEqual(TurnInfo(Usage(input=1, output=5, output_ns=2_000_000_000)).summary(),
                         "1 in · 5 out · 6 tokens · 2.5 tok/s")

    def test_summary_names_the_tool_groups_sent(self):
        self.assertEqual(TurnInfo(Usage(input=1), tool_groups=["cs", "anime"]).summary(),
                         "1 in · 0 out · 1 token · tools: cs, anime")

    def test_summary_says_none_when_the_router_chose_no_tools(self):
        self.assertEqual(TurnInfo(Usage(input=1), tool_groups=[]).summary(), "1 in · 0 out · 1 token · tools: none")

if __name__ == "__main__":
    unittest.main()
