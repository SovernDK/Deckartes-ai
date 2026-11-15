"""What the web tools send to Ollama's web API and give the model, from fake responses."""

import json
import unittest
from unittest import mock

from core.config import config
from http_fake import HttpTest
from tools import tools


class WebToolTest(HttpTest):
    def setUp(self):
        patcher = mock.patch.object(config, "ollama_api_key", "key")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_search_sends_the_query_and_cuts_long_snippets(self):
        server = self.serve(
            {
                "/api/web_search": {
                    "results": [{"title": "Textual", "url": "https://textual.textualize.io", "content": "y" * 2000}]
                }
            }
        )
        results = await tools.search_web("textual")
        self.assertEqual(json.loads(server.requests[0].content), {"query": "textual", "max_results": 5})
        self.assertEqual(server.requests[0].headers["authorization"], "Bearer key")
        self.assertEqual(results[0]["content"], "y" * 1500 + " [...]")

    async def test_fetched_page_caps_its_text_and_links(self):
        links = [f"https://example.com/{i}" for i in range(30)]
        self.serve({"/api/web_fetch": {"title": "Long", "content": "y" * 9000, "links": links}})
        page = await tools.fetch_web_page("https://example.com")
        self.assertEqual(page, {"title": "Long", "content": "y" * 8000 + "\n[...page truncated]", "links": links[:20]})

    async def test_search_without_a_key_says_how_to_set_it(self):
        with mock.patch.object(config, "ollama_api_key", ""):
            self.assertEqual(
                await tools.run_tool("search_web", {"query": "x"}),
                "Tool search_web failed: No Ollama web API token: set OLLAMA_API_KEY in the environment or .env",
            )


if __name__ == "__main__":
    unittest.main()
