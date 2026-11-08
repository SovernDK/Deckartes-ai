"""Tests for the shared Ollama clients: one per kind and host."""

import unittest
from unittest import mock

import ollama_client
from config import config

class ClientsTest(unittest.TestCase):
    def setUp(self):
        ollama_client._clients.clear()
        self.addCleanup(ollama_client._clients.clear)

    def test_the_same_client_is_reused(self):
        self.assertIs(ollama_client.get_client(), ollama_client.get_client())
        self.assertIs(ollama_client.get_async_client(), ollama_client.get_async_client())

    def test_sync_and_async_clients_are_separate(self):
        self.assertIsNot(ollama_client.get_client(), ollama_client.get_async_client())

    def test_a_new_host_gets_a_new_client(self):
        first = ollama_client.get_async_client()
        with mock.patch.object(config, "host", "http://elsewhere:11434"):
            self.assertIsNot(ollama_client.get_async_client(), first)
        self.assertIs(ollama_client.get_async_client(), first)

if __name__ == "__main__":
    unittest.main()
