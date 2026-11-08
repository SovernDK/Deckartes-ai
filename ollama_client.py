"""The shared Ollama clients. Imports nothing else from the app, so any module can use them."""

import ollama

from config import config

_clients = {}

def get_client() -> ollama.Client:
    """Sync client for the current config.host, recreated if the host changes."""
    if ("sync", config.host) not in _clients:
        _clients[("sync", config.host)] = ollama.Client(host=config.host)
    return _clients[("sync", config.host)]

def get_async_client() -> ollama.AsyncClient:
    """Async client for the current config.host, recreated if the host changes."""
    if ("async", config.host) not in _clients:
        _clients[("async", config.host)] = ollama.AsyncClient(host=config.host)
    return _clients[("async", config.host)]
