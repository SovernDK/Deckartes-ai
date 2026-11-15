"""Starts and stops the local Rapid-MLX servers, which run as child processes.

The chat server (config.host) holds the chat model and the routing embedding model. A second,
embeddings-only server (config.file_embed_host) holds config.rag_embed_model and is started only
when the library needs it, because a Rapid-MLX server can serve one embedding model. The decision
server (config.decision_host, `rapid-mlx system-one`) holds config.decision_model, which labels
spending payees; it's started for a labeling and stopped after it, since the model takes 2 GB.

A server already answering on a port is used as it is and never stopped, which also covers one
left behind by a crash. Servers started here stop when the app exits. Their output goes to a log
file, never the terminal the app draws on.
"""

import atexit
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from core.config import config

READY_PATH = "/health/ready"
POLL_SECONDS = 0.5
# Long, because the first start downloads the model
START_LIMIT_SECONDS = 30 * 60
STOP_WAIT_SECONDS = 10
LOG_DIR = Path.home() / ".cache" / "deckartes"
# Parsers for models Rapid-MLX can't identify by itself, by lowercase start of the model id
PARSER_FLAGS = {
    "liquidai/lfm3": ["--tool-call-parser", "nemotron", "--reasoning-parser", "qwen3"],
}


class ModelServerError(Exception):
    """A model server couldn't be started or reached, or refused a request. The message says why."""


class _Server:
    def __init__(
        self, name: str, url: Callable[[], str], arguments: Callable[[], list[str]], ready_path: str = READY_PATH
    ) -> None:
        self.name = name
        self.url = url
        self.arguments = arguments
        # The decision server answers /health only once its model has loaded
        self.ready_path = ready_path
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"rapid-mlx-{self.name.replace(' ', '-')}.log"

    def status(self) -> int | None:
        try:
            return httpx.get(self.url() + self.ready_path, timeout=2).status_code
        except httpx.HTTPError:
            return None

    def _start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        program = os.path.expanduser(config.mlx_server_command)
        try:
            with open(self.log_path, "w") as log:
                # Its own process group, so stop() also reaches anything it starts
                self.process = subprocess.Popen(
                    [program, *self.arguments()],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as e:
            raise ModelServerError(
                f"Could not start {program}: {e}. Install it with: uv tool install 'rapid-mlx[embeddings]'"
            ) from e

    def _wait_until_ready(self) -> None:
        process = self.process
        deadline = time.monotonic() + START_LIMIT_SECONDS
        while time.monotonic() < deadline:
            status = self.status()
            if status == 200:
                return
            if process is not None and process.poll() is not None:
                raise ModelServerError(f"The {self.name} server stopped while starting. See {self.log_path}")
            if process is None and status is None:
                raise ModelServerError(f"The {self.name} server at {self.url()} stopped answering")
            time.sleep(POLL_SECONDS)
        raise ModelServerError(
            f"The {self.name} server wasn't ready after {START_LIMIT_SECONDS // 60} minutes. See {self.log_path}"
        )

    def ensure_running(self) -> None:
        with self.lock:
            status = self.status()
            if status == 200:
                return
            if self.process is not None and self.process.poll() is not None:
                self.process = None
            # Any other status is a server still loading: wait for it instead of starting another
            if status is None and self.process is None:
                self._start()
            self._wait_until_ready()

    def stop(self) -> bool:
        """Stop this server if this app started it. Returns whether it did."""
        # No lock: this has to be able to end a start that is still waiting
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return False
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return True


def _address(url: str) -> list[str]:
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        raise ValueError(f"Model server URL {url!r} needs a host and a port")
    return ["--host", parts.hostname, "--port", str(parts.port)]


def _chat_arguments() -> list[str]:
    model = config.model
    parsers = next((flags for start, flags in PARSER_FLAGS.items() if model.lower().startswith(start)), [])
    return [
        "serve",
        model,
        *_address(config.host),
        "--yes",
        "--enable-auto-tool-choice",
        *parsers,
        "--embedding-model",
        config.embed_model,
    ]


def _file_arguments() -> list[str]:
    return ["serve", *_address(config.file_embed_host), "--yes", "--embedding-model", config.rag_embed_model]


def _decision_arguments() -> list[str]:
    model = config.decision_model
    if not model.is_dir():
        raise ModelServerError(
            f"No decision model at {model}. Make it with: "
            f"~/.local/share/uv/tools/rapid-mlx/bin/python src/model/quantize_decider.py {model}"
        )
    return ["system-one", str(model), "--backend", "decider", *_address(config.decision_host)]


_chat = _Server("chat", lambda: config.host, _chat_arguments)
_files = _Server("file search", lambda: config.file_embed_host, _file_arguments)
_decisions = _Server("decision", lambda: config.decision_host, _decision_arguments, ready_path="/health")


def ensure_chat() -> None:
    _chat.ensure_running()


def ensure_files() -> None:
    _files.ensure_running()


def ensure_decisions() -> None:
    _decisions.ensure_running()


def stop_decisions() -> bool:
    return _decisions.stop()


def chat_is_ready() -> bool:
    return _chat.status() == 200


def restart_chat() -> None:
    _chat.stop()
    _chat.ensure_running()


def stop_all() -> list[str]:
    return [server.name for server in (_chat, _files, _decisions) if server.stop()]


atexit.register(stop_all)
