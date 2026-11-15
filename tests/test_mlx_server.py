"""mlx_server with a small fake server program as the real child process."""

import http.server
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from core.config import config
from model import mlx_server

FAKE_SERVER = f"""#!{sys.executable}
import http.server, os, sys
with open(sys.argv[0] + ".args", "w") as out:
    out.write(" ".join(sys.argv[1:]))
with open(sys.argv[0] + ".pids", "a") as out:
    print(os.getpid(), file=out)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), Handler).serve_forever()
"""
FAILING_SERVER = f"#!{sys.executable}\nprint('no such model')\nraise SystemExit(1)\n"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.url = f"http://127.0.0.1:{free_port()}"
        self.files_url = f"http://127.0.0.1:{free_port()}"
        self.decisions_url = f"http://127.0.0.1:{free_port()}"
        self.decision_model = self.folder / "decider-2b-4bit"
        self.decision_model.mkdir()
        for target, name, value in [
            (config, "host", self.url),
            (config, "file_embed_host", self.files_url),
            (config, "decision_host", self.decisions_url),
            (config, "decision_model", self.decision_model),
            (config, "mlx_server_command", self.program(FAKE_SERVER)),
            (config, "model", "some/model"),
            (mlx_server, "LOG_DIR", self.folder / "logs"),
            (mlx_server, "POLL_SECONDS", 0.02),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(mlx_server.stop_all)

    def program(self, source: str) -> str:
        path = self.folder / "server"
        path.write_text(source)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def arguments(self) -> str:
        return Path(config.mlx_server_command + ".args").read_text()

    def launches(self) -> list[int]:
        """The process id of every server program started, in order."""
        pids = Path(config.mlx_server_command + ".pids")
        return [int(line) for line in pids.read_text().split()] if pids.exists() else []

    def answers(self) -> bool:
        try:
            return httpx.get(self.url + mlx_server.READY_PATH, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def start_other_server(self, status=200) -> None:
        """Something this app didn't start, already on the chat port."""

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", int(self.url.rsplit(":", 1)[1])), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def test_starts_when_nothing_answers_and_stops_on_request(self):
        mlx_server.ensure_chat()
        self.assertTrue(mlx_server.chat_is_ready())
        self.assertEqual(mlx_server.stop_all(), ["chat"])
        self.assertFalse(self.answers())

    def test_second_call_keeps_the_same_process(self):
        mlx_server.ensure_chat()
        mlx_server.ensure_chat()
        self.assertEqual(len(self.launches()), 1)

    def test_crashed_server_is_started_again(self):
        mlx_server.ensure_chat()
        os.kill(self.launches()[0], signal.SIGKILL)
        while self.answers():
            pass
        mlx_server.ensure_chat()
        self.assertEqual(len(self.launches()), 2)
        self.assertTrue(mlx_server.chat_is_ready())

    def test_running_server_is_reused_and_never_stopped(self):
        self.start_other_server()
        mlx_server.ensure_chat()
        self.assertEqual(self.launches(), [])
        self.assertEqual(mlx_server.stop_all(), [])
        self.assertTrue(self.answers())

    def test_failed_start_names_the_log(self):
        with (
            mock.patch.object(config, "mlx_server_command", self.program(FAILING_SERVER)),
            self.assertRaisesRegex(mlx_server.ModelServerError, "stopped while starting. See .*rapid-mlx-chat.log"),
        ):
            mlx_server.ensure_chat()
        self.assertIn("no such model", (self.folder / "logs" / "rapid-mlx-chat.log").read_text())

    def test_missing_program_says_how_to_install(self):
        with (
            mock.patch.object(config, "mlx_server_command", str(self.folder / "nothing")),
            self.assertRaisesRegex(mlx_server.ModelServerError, "uv tool install"),
        ):
            mlx_server.ensure_chat()

    def test_chat_server_gets_the_model_and_the_routing_embedder(self):
        mlx_server.ensure_chat()
        port = self.url.rsplit(":", 1)[1]
        self.assertEqual(
            self.arguments(),
            f"serve some/model --host 127.0.0.1 --port {port} --yes "
            f"--enable-auto-tool-choice --embedding-model {config.embed_model}",
        )

    def test_lfm3_gets_its_parsers(self):
        with mock.patch.object(config, "model", "LiquidAI/LFM3-1.7B-MLX-4bit"):
            mlx_server.ensure_chat()
        self.assertIn("--tool-call-parser nemotron --reasoning-parser qwen3", self.arguments())

    def test_file_server_is_embeddings_only(self):
        mlx_server.ensure_files()
        port = self.files_url.rsplit(":", 1)[1]
        self.assertEqual(
            self.arguments(), f"serve --host 127.0.0.1 --port {port} --yes --embedding-model {config.rag_embed_model}"
        )
        self.assertEqual(mlx_server.stop_all(), ["file search"])

    def test_decision_server_holds_the_decision_model_until_stopped(self):
        mlx_server.ensure_decisions()
        port = self.decisions_url.rsplit(":", 1)[1]
        self.assertEqual(
            self.arguments(), f"system-one {self.decision_model} --backend decider --host 127.0.0.1 --port {port}"
        )
        self.assertTrue(mlx_server.stop_decisions())
        self.assertEqual(mlx_server.stop_all(), [])

    def test_missing_decision_model_says_how_to_make_it(self):
        self.decision_model.rmdir()
        with self.assertRaisesRegex(
            mlx_server.ModelServerError,
            f"^No decision model at {self.decision_model}. Make it with: .*quantize_decider.py",
        ):
            mlx_server.ensure_decisions()
        self.assertEqual(self.launches(), [])

    def test_restart_uses_the_new_model(self):
        mlx_server.ensure_chat()
        with mock.patch.object(config, "model", "other/model"):
            mlx_server.restart_chat()
        self.assertEqual(len(self.launches()), 2)
        self.assertIn("serve other/model", self.arguments())

    def test_stop_ends_a_start_that_is_still_waiting(self):
        slow = (
            f"#!{sys.executable}\nimport os, sys, time\n"
            "with open(sys.argv[0] + '.pids', 'a') as out:\n    print(os.getpid(), file=out)\ntime.sleep(60)\n"
        )
        errors = []

        def start():
            try:
                mlx_server.ensure_chat()
            except mlx_server.ModelServerError as e:
                errors.append(str(e))

        with mock.patch.object(config, "mlx_server_command", self.program(slow)):
            thread = threading.Thread(target=start)
            thread.start()
            while not self.launches():
                pass
            self.assertEqual(mlx_server.stop_all(), ["chat"])
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIn("stopped while starting", errors[0])


if __name__ == "__main__":
    unittest.main()
