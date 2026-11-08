"""Tests for ollama_api.stream_response with a fake Ollama client: the events a turn yields."""

import unittest
from types import SimpleNamespace as NS
from unittest import mock

import ollama_api
from ollama_api import Failed, Text, ToolsDone, ToolStarted, TurnInfo
from tool_router import Selection

def chunk(content="", tool_calls=None, done=False):
    return NS(message=NS(content=content, tool_calls=tool_calls), done=done,
              prompt_eval_count=10 if done else None, eval_count=5 if done else None,
              eval_duration=10**9 if done else None)

def call(tool, **arguments):
    return NS(function=NS(name=tool, arguments=arguments))

class FakeClient:
    """Answers each chat call with the next round's chunks (an exception among them is raised
    there), and records what each call sent."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.sent = []

    async def chat(self, **kwargs):
        self.sent.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.rounds:
            raise ConnectionError("no more rounds")
        chunks = self.rounds.pop(0)
        async def stream():
            for c in chunks:
                if isinstance(c, Exception):
                    raise c
                yield c
        return stream()

class StreamResponseTest(unittest.IsolatedAsyncioTestCase):
    def use(self, client, selection=Selection(None, None)):
        for patcher in [mock.patch.object(ollama_api, "get_async_client", return_value=client),
                        mock.patch.object(ollama_api, "supports_tools", mock.AsyncMock(return_value=True)),
                        mock.patch.object(ollama_api, "select_tools", mock.AsyncMock(return_value=selection)),
                        mock.patch.object(ollama_api, "run_tool", mock.AsyncMock(return_value="tool result"))]:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def events(self, turn=None):
        messages = [{"role": "user", "content": "q"}]
        return [event async for event in ollama_api.stream_response(messages, turn)]

    async def test_text_chunks(self):
        self.use(FakeClient([chunk("Hel"), chunk("lo", done=True)]))
        turn = TurnInfo()
        self.assertEqual(await self.events(turn), [Text("Hel"), Text("lo")])
        self.assertEqual((turn.usage.input, turn.usage.output), (10, 5))

    async def test_tool_round(self):
        client = FakeClient(
            [chunk("Let me check."), chunk(tool_calls=[call("get_cs_team", name="NAVI")], done=True)],
            [chunk("NAVI is ranked 3rd.", done=True)],
        )
        self.use(client)
        turn = TurnInfo()
        self.assertEqual(await self.events(turn), [
            Text("Let me check."), Text("\n\n"), ToolStarted("get_cs_team"), ToolsDone(),
            Text("NAVI is ranked 3rd."),
        ])
        ollama_api.run_tool.assert_awaited_once_with("get_cs_team", {"name": "NAVI"})
        self.assertEqual(client.sent[1]["messages"][-1],
                         {"role": "tool", "tool_name": "get_cs_team", "content": "tool result"})
        self.assertEqual(turn.usage.input, 20)

    async def test_tool_round_without_text_has_no_break(self):
        self.use(FakeClient([chunk(tool_calls=[call("a"), call("b")], done=True)], [chunk("ok", done=True)]))
        self.assertEqual(await self.events(),
                         [ToolStarted("a"), ToolStarted("b"), ToolsDone(), Text("ok")])

    async def test_selected_tools_are_sent(self):
        client = FakeClient([chunk("ok", done=True)])
        self.use(client, Selection(["T"], ["cs"]))
        turn = TurnInfo()
        await self.events(turn)
        self.assertEqual(client.sent[0]["tools"], ["T"])
        self.assertEqual(turn.tool_groups, ["cs"])

    async def test_failure_is_the_last_event(self):
        self.use(FakeClient([chunk("partial"), ConnectionError("connection lost")]))
        self.assertEqual(await self.events(), [Text("partial"), Failed("connection lost")])

    async def test_failure_before_any_text(self):
        self.use(FakeClient())
        self.assertEqual(await self.events(), [Failed("no more rounds")])

    async def test_error_looking_reply_is_text(self):
        self.use(FakeClient([chunk("**Error:** is how markdown bolds a label", done=True)]))
        self.assertEqual(await self.events(), [Text("**Error:** is how markdown bolds a label")])

    async def test_too_many_tool_rounds(self):
        rounds = [[chunk(tool_calls=[call("a")], done=True)] for _ in range(ollama_api.MAX_TOOL_ROUNDS)]
        self.use(FakeClient(*rounds))
        events = await self.events()
        self.assertEqual(events[-1], Failed(f"model still calling tools after {ollama_api.MAX_TOOL_ROUNDS} rounds"))
        self.assertEqual(events.count(ToolsDone()), ollama_api.MAX_TOOL_ROUNDS)

if __name__ == "__main__":
    unittest.main()
