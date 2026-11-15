"""mlx_client against MockTransport, with SSE text in the shape the real Rapid-MLX server sends."""

import json
import unittest
from unittest import mock

import httpx

from core.config import config
from model import mlx_client, mlx_server
from model.mlx_client import Delta, ModelServerError, TokenCount, ToolCall


def sse(*chunks) -> str:
    lines = [": keepalive\n\n"]
    lines += [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields}]}


FINISH = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "metrics": {"mean_itl_ms": 20.0}}
USAGE = {"choices": [], "usage": {"prompt_tokens": 34, "completion_tokens": 8, "total_tokens": 42}}


async def tool_function(name: str) -> str:
    """Look something up.

    Args:
        name: What to look up
    """
    return name


class ClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.response = httpx.Response(200, text=sse())
        transport = httpx.MockTransport(self.handle)
        for target, name, value in [
            (mlx_client, "_transport", transport),
            (mlx_client, "_sync_transport", transport),
            (mlx_server, "ensure_chat", mock.Mock()),
            (mlx_server, "ensure_files", mock.Mock()),
            (mlx_server, "ensure_decisions", mock.Mock()),
            (config, "model", "some/model"),
            (config, "temperature", 0.1),
            (config, "top_k", 50),
            (config, "repetition_penalty", 1.1),
            (config, "max_tokens", 4096),
            (config, "enable_thinking", False),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    def serve(self, *chunks) -> None:
        self.response = httpx.Response(200, text=sse(*chunks))

    async def stream(self, tools=None) -> list[Delta]:
        return [d async for d in mlx_client.stream_chat([{"role": "user", "content": "hi"}], tools)]


class StreamChatTest(ClientTest):
    async def test_text_then_a_last_piece_with_tokens_and_speed(self):
        self.serve(delta(role="assistant"), delta(content="Hel"), delta(content="lo"), FINISH, USAGE)
        self.assertEqual(await self.stream(), [Delta("Hel"), Delta("lo"), Delta(usage=TokenCount(34, 8, 160_000_000))])

    async def test_reasoning_comes_apart_from_the_reply(self):
        self.serve(delta(reasoning_content="hmm"), delta(reasoning=" so"), delta(content="ok"))
        self.assertEqual(await self.stream(), [Delta(reasoning="hmm"), Delta(reasoning=" so"), Delta("ok"), Delta()])

    async def test_sends_the_settings_and_asks_for_usage(self):
        await self.stream()
        self.assertEqual(
            self.body(),
            {
                "model": "some/model",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4096,
                "enable_thinking": False,
                "temperature": 0.1,
                "top_k": 50,
                "repetition_penalty": 1.1,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )

    async def test_setting_left_to_the_model_is_not_sent(self):
        with mock.patch.object(config, "temperature", None):
            await self.stream()
        self.assertNotIn("temperature", self.body())

    async def test_tools_are_sent_as_definitions(self):
        await self.stream([tool_function])
        self.assertEqual(self.body()["tools"][0]["function"]["name"], "tool_function")

    async def test_whole_tool_call_in_one_chunk(self):
        self.serve(
            delta(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_cs_team", "arguments": '{"name": "Spirit"}'},
                    }
                ],
                content=None,
            )
        )
        self.assertEqual((await self.stream())[-1].tool_calls, [ToolCall("call_1", "get_cs_team", {"name": "Spirit"})])

    async def test_tool_call_in_pieces_and_two_calls_in_order(self):
        self.serve(
            delta(tool_calls=[{"index": 1, "id": "call_b", "function": {"name": "b", "arguments": "{}"}}]),
            delta(tool_calls=[{"index": 0, "id": "call_a", "function": {"name": "a", "arguments": '{"x"'}}]),
            delta(tool_calls=[{"index": 0, "function": {"arguments": ": 1}"}}]),
        )
        self.assertEqual(
            (await self.stream())[-1].tool_calls, [ToolCall("call_a", "a", {"x": 1}), ToolCall("call_b", "b", {})]
        )

    async def test_arguments_that_are_not_json_are_none(self):
        self.serve(delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "a", "arguments": "{oops"}}]))
        self.assertEqual((await self.stream())[-1].tool_calls, [ToolCall("c", "a", None)])

    async def test_status_error_uses_the_server_message(self):
        self.response = httpx.Response(400, json={"error": {"message": "bad model"}})
        with self.assertRaisesRegex(ModelServerError, "^The model server responded with status 400: bad model$"):
            await self.stream()

    async def test_status_error_without_json_shows_the_body(self):
        self.response = httpx.Response(500, text="boom")
        with self.assertRaisesRegex(ModelServerError, "status 500: boom$"):
            await self.stream()

    async def test_error_in_the_stream_is_raised(self):
        self.serve({"error": {"message": "out of memory"}})
        with self.assertRaisesRegex(ModelServerError, "^The model server failed: out of memory$"):
            await self.stream()

    async def test_unreachable_server_names_its_address(self):
        self.response = httpx.ConnectError("refused")
        with self.assertRaisesRegex(ModelServerError, "^Could not reach the model server at "):
            await self.stream()

    async def test_makes_sure_the_server_runs(self):
        await self.stream()
        mlx_server.ensure_chat.assert_called_once_with()


class CompleteTest(ClientTest):
    async def test_complete_returns_the_reply_and_token_counts(self):
        self.response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "gist"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
        )
        reply = await mlx_client.complete([{"role": "user", "content": "x"}])
        self.assertEqual(reply, mlx_client.Reply("gist", TokenCount(9, 2, 0)))
        self.assertNotIn("stream", self.body())

    async def test_the_summary_is_written_without_thinking(self):
        self.response = httpx.Response(200, json={"choices": [{"message": {"content": "gist"}}]})
        with mock.patch.object(config, "enable_thinking", True):
            await mlx_client.complete([{"role": "user", "content": "x"}])
        self.assertIs(self.body()["enable_thinking"], False)


class EmbedTest(ClientTest):
    def setUp(self):
        super().setUp()
        self.response = httpx.Response(
            200, json={"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
        )

    async def test_routing_uses_the_chat_server_and_keeps_the_order(self):
        self.assertEqual(await mlx_client.embed(["a", "b"]), [[1.0], [2.0]])
        self.assertEqual(str(self.requests[-1].url), f"{config.host}/v1/embeddings")
        self.assertEqual(self.body(), {"model": config.embed_model, "input": ["a", "b"]})
        mlx_server.ensure_files.assert_not_called()

    def test_file_text_uses_the_second_server(self):
        self.assertEqual(mlx_client.embed_file_text(["a", "b"]), [[1.0], [2.0]])
        self.assertEqual(str(self.requests[-1].url), f"{config.file_embed_host}/v1/embeddings")
        self.assertEqual(self.body()["model"], config.rag_embed_model)
        mlx_server.ensure_files.assert_called_once_with()

    def test_file_text_error_uses_the_server_message(self):
        self.response = httpx.Response(400, json={"error": {"message": "too long"}})
        with self.assertRaisesRegex(ModelServerError, "too long"):
            mlx_client.embed_file_text(["a"])


class DecideTest(ClientTest):
    async def test_asks_the_decision_server_one_choice_and_returns_its_probabilities(self):
        self.response = httpx.Response(
            200, json={"answers": {"answer": {"type": "choice", "choice": "a", "probabilities": {"a": 0.7, "b": 0.3}}}}
        )

        probabilities = await mlx_client.decide("state", "Which?", {"a": "first", "b": "second"})

        self.assertEqual(probabilities, {"a": 0.7, "b": 0.3})
        self.assertEqual(str(self.requests[-1].url), f"{config.decision_host}/v1/systemone")
        self.assertEqual(
            self.body(),
            {
                "model": config.decision_model.name,
                "state": "state",
                "questions": {
                    "answer": {"type": "choice", "instructions": "Which?", "criteria": {"a": "first", "b": "second"}}
                },
            },
        )
        mlx_server.ensure_decisions.assert_called_once_with()

    async def test_decision_error_uses_the_server_message(self):
        self.response = httpx.Response(422, json={"error": {"message": "choice criteria must be a non-empty object"}})
        with self.assertRaisesRegex(ModelServerError, "non-empty object"):
            await mlx_client.decide("state", "Which?", {})


class ListModelsTest(unittest.TestCase):
    def test_complete_chat_models_only_and_the_current_one(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            hub = Path(folder) / "hub"

            def repo(name, *files):
                snapshot = hub / f"models--{name.replace('/', '--')}" / "snapshots" / "abc"
                snapshot.mkdir(parents=True)
                for file in files:
                    (snapshot / file).touch()

            repo("org/chat-model", "config.json", "model.safetensors")
            repo("org/partial", "config.json")
            repo("org/some-embedding", "config.json", "model.safetensors")
            repo(config.embed_model, "config.json", "model.safetensors")
            with (
                mock.patch.dict("os.environ", {"HF_HUB_CACHE": str(hub)}),
                mock.patch.object(config, "model", "a/current"),
            ):
                self.assertEqual(mlx_client.list_models(), ["a/current", "org/chat-model"])


if __name__ == "__main__":
    unittest.main()
