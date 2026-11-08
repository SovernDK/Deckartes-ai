"""Tests for http_client.request, and for each tool backend through it, with httpx.MockTransport
in place of the network (http_client._transport)."""

import json
import unittest
from unittest import mock

import httpx

import http_client
import tools
from config import config
from http_fake import HttpTest

class BackendError(Exception):
    pass

class RequestTest(HttpTest):
    async def get(self, **kwargs):
        return await http_client.request("GET", "https://api.test/items", name="Test", error=BackendError, **kwargs)

    async def test_json_with_token(self):
        server = self.serve({"/items": [1, 2]})
        res = await self.get(params={"page": 2}, token="secret", token_env="TEST_TOKEN")
        self.assertEqual(res.json(), [1, 2])
        self.assertEqual(server.requests[0].headers["authorization"], "Bearer secret")
        self.assertEqual(server.requests[0].url.params["page"], "2")

    async def test_missing_token(self):
        server = self.serve({"/items": []})
        with self.assertRaisesRegex(BackendError, r"^No Test token: set TEST_TOKEN in the environment or \.env$"):
            await self.get(token="", token_env="TEST_TOKEN")
        self.assertEqual(server.requests, [])

    async def test_cached_until_max_age(self):
        server = self.serve({"/items": [1]})
        with mock.patch.object(http_client.time, "monotonic", return_value=100.0):
            await self.get(max_age=60)
            await self.get(max_age=60)
        self.assertEqual(len(server.requests), 1)
        with mock.patch.object(http_client.time, "monotonic", return_value=161.0):
            await self.get(max_age=60)
        self.assertEqual(len(server.requests), 2)

    async def test_params_are_part_of_the_cache_key(self):
        server = self.serve({"/items": [1]})
        await self.get(params={"page": 1}, max_age=60)
        await self.get(params={"page": 2}, max_age=60)
        self.assertEqual(len(server.requests), 2)

    async def test_not_cached_without_max_age(self):
        server = self.serve({"/items": [1]})
        await self.get()
        await self.get()
        self.assertEqual(len(server.requests), 2)

    async def test_failures_are_not_cached(self):
        server = self.serve({"/items": httpx.Response(500, text="oops")})
        for _ in range(2):
            with self.assertRaises(BackendError):
                await self.get(max_age=60)
        self.assertEqual(len(server.requests), 2)

    async def test_unreachable(self):
        self.serve({"/items": httpx.ConnectError("connection refused")})
        with self.assertRaisesRegex(BackendError, r"^Could not reach Test: connection refused$"):
            await self.get()

    async def test_rate_limit_with_reset_time(self):
        reset = 1_800_000_000
        self.serve({"/items": httpx.Response(429, headers={"x-ratelimit-reset": str(reset)})})
        when = __import__("datetime").datetime.fromtimestamp(reset).strftime("%H:%M:%S")
        with self.assertRaisesRegex(BackendError, rf"^Test rate limit reached, resets at {when}$"):
            await self.get()

    async def test_rate_limit_without_reset_time(self):
        self.serve({"/items": httpx.Response(429)})
        with self.assertRaisesRegex(BackendError, r"^Test rate limit reached, try again later$"):
            await self.get()

    async def test_status_message(self):
        self.serve({"/items": httpx.Response(403, text="forbidden")})
        with self.assertRaisesRegex(BackendError, r"^Not in your plan$"):
            await self.get(status_messages={403: "Not in your plan"})

    async def test_other_status_shows_the_start_of_the_body(self):
        self.serve({"/items": httpx.Response(500, text="x" * 500)})
        with self.assertRaises(BackendError) as caught:
            await self.get()
        self.assertEqual(str(caught.exception), f"Test responded with status 500: {'x' * 200}")

    async def test_post_json(self):
        server = self.serve({"/items": {"ok": True}})
        res = await http_client.request("POST", "https://api.test/items", name="Test", error=BackendError,
                                        json={"query": "hi"})
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(server.requests[0].method, "POST")
        self.assertEqual(json.loads(server.requests[0].content), {"query": "hi"})

class BackendTest(HttpTest):
    """One tool per backend, end to end through http_client."""

    async def test_glances(self):
        self.serve({"/api/4/cpu": {"total": 12.5, "user": 8, "system": 4.5, "cpucore": 4}})
        self.assertEqual(await tools.get_cpu_usage(), "CPU: 12.5% used (user 8.0%, system 4.5%), 4 cores")

    async def test_glances_unreachable(self):
        self.serve({"/api/4/cpu": httpx.ConnectError("no route to host")})
        self.assertEqual(await tools.run_tool("get_cpu_usage", {}),
                         "Tool get_cpu_usage failed: Could not reach Glances: no route to host")

    async def test_web_search(self):
        server = self.serve({"/api/web_search": {"results": [
            {"title": "Textual", "url": "https://textual.textualize.io", "content": "y" * 2000}]}})
        with mock.patch.object(config, "ollama_api_key", "key"):
            results = await tools.search_web("textual")
        self.assertEqual(json.loads(server.requests[0].content), {"query": "textual", "max_results": 5})
        self.assertEqual(server.requests[0].headers["authorization"], "Bearer key")
        self.assertEqual(results[0]["content"], "y" * tools.MAX_SNIPPET_CHARS + " [...]")

    async def test_web_search_without_key(self):
        with mock.patch.object(config, "ollama_api_key", ""):
            self.assertEqual(await tools.run_tool("search_web", {"query": "x"}),
                             "Tool search_web failed: No Ollama web API token: set OLLAMA_API_KEY in the environment or .env")

    async def test_cs_ranking(self):
        year = __import__("datetime").date.today().year
        self.serve({
            f"/repos/ValveSoftware/counter-strike_regional_standings/contents/live/{year}": [
                {"name": f"standings_global_{year}_09_01.md"}, {"name": f"standings_global_{year}_10_06.md"},
                {"name": f"standings_europe_{year}_10_06.md"}],
            f"/ValveSoftware/counter-strike_regional_standings/main/live/{year}/standings_global_{year}_10_06.md":
                "| Standing | Points | Team Name | Roster |\n| --- | --- | --- | --- |\n"
                "| 1 | 2010 | Vitality | apEX, ZywOo |\n| 2 | 1900 | MOUZ | Brollan, torzsi |\n",
        })
        self.assertEqual(await tools.get_cs_ranking("global", 1),
                         f"Valve Regional Standings (global), as of {year}-10-06:\n1. Vitality: 2010 points (apEX, ZywOo)")

    async def test_pandascore_plan_limit(self):
        self.serve({"/csgo/players": httpx.Response(403, text="forbidden")})
        with mock.patch.object(config, "pandascore_token", "token"):
            self.assertEqual(await tools.run_tool("get_cs_player", {"name": "ZywOo"}),
                             "Tool get_cs_player failed: Access denied: this data isn't included in the PandaScore plan")

    async def test_anime_season(self):
        server = self.serve({"/api/v3/anime": {"totalAmount": 2, "anime": [
            {"title": "Sousou no Frieren", "names": {"english": "Frieren"}, "episodes": 28,
             "stats": {"ratingCount": 900, "averageScore": 91}},
            {"title": "Weak Show", "stats": {"ratingCount": 50, "averageScore": 40}},
        ]}})
        with mock.patch.object(config, "animeschedule_token", "token"), \
             mock.patch.object(config, "anime_min_rating", 6.0):
            text = await tools.list_season_anime("fall", 2026)
        self.assertIn("1. Frieren: rated 9.1, 28 episodes", text)
        self.assertNotIn("Weak Show", text)
        self.assertEqual(server.requests[0].url.params["seasons"], "fall")
        self.assertEqual(server.requests[0].headers["authorization"], "Bearer token")

if __name__ == "__main__":
    unittest.main()
