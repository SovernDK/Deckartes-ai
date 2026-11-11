"""Tests for http_client.request and its paging, with httpx.MockTransport in place of the network
(http_client._transport). Each backend's own requests and errors are tested with its tools."""

import json
import unittest
from unittest import mock

import httpx

from core import http_client
from core.config import config
from http_fake import HttpTest


class BackendError(Exception):
    pass

class RequestTest(HttpTest):
    async def get(self, **kwargs):
        return await http_client.request("GET", "https://api.test/items", name="Test", error=BackendError, **kwargs)

    async def test_token_and_params_are_sent(self):
        server = self.serve({"/items": [1, 2]})
        res = await self.get(params={"page": 2}, token="secret", token_env="TEST_TOKEN")
        self.assertEqual(res.json(), [1, 2])
        self.assertEqual(server.requests[0].headers["authorization"], "Bearer secret")
        self.assertEqual(server.requests[0].url.params["page"], "2")

    async def test_missing_token_names_its_variable_without_a_request(self):
        server = self.serve({"/items": []})
        with self.assertRaisesRegex(BackendError, r"^No Test token: set TEST_TOKEN in the environment or \.env$"):
            await self.get(token="", token_env="TEST_TOKEN")
        self.assertEqual(server.requests, [])

    async def test_response_is_cached_until_max_age(self):
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

    async def test_response_is_not_cached_without_max_age(self):
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

    async def test_unreachable_server_names_the_backend(self):
        self.serve({"/items": httpx.ConnectError("connection refused")})
        with self.assertRaisesRegex(BackendError, r"^Could not reach Test: connection refused$"):
            await self.get()

    async def test_rate_limit_gives_the_local_reset_time(self):
        # 2027-01-15 08:00:00 UTC
        self.serve({"/items": httpx.Response(429, headers={"x-ratelimit-reset": "1800000000"})})
        with mock.patch.object(config, "timezone", "Asia/Tokyo"), \
             self.assertRaisesRegex(BackendError, r"^Test rate limit reached, resets at 17:00:00$"):
            await self.get()

    async def test_rate_limit_without_reset_says_try_later(self):
        self.serve({"/items": httpx.Response(429)})
        with self.assertRaisesRegex(BackendError, r"^Test rate limit reached, try again later$"):
            await self.get()

    async def test_status_message_replaces_the_default_error(self):
        self.serve({"/items": httpx.Response(403, text="forbidden")})
        with self.assertRaisesRegex(BackendError, r"^Not in your plan$"):
            await self.get(status_messages={403: "Not in your plan"})

    async def test_other_status_shows_the_start_of_the_body(self):
        self.serve({"/items": httpx.Response(500, text="x" * 500)})
        with self.assertRaises(BackendError) as caught:
            await self.get()
        self.assertEqual(str(caught.exception), f"Test responded with status 500: {'x' * 200}")

    async def test_post_sends_the_json_body(self):
        server = self.serve({"/items": {"ok": True}})
        res = await http_client.request("POST", "https://api.test/items", name="Test", error=BackendError,
                                        json={"query": "hi"})
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(server.requests[0].method, "POST")
        self.assertEqual(json.loads(server.requests[0].content), {"query": "hi"})

class PagesTest(unittest.IsolatedAsyncioTestCase):
    async def collect(self, pages: list, *, total=None, page_size=2, max_pages=5):
        """The pages yielded, and the indexes fetched, from a list of pages."""
        self.fetched = []
        async def fetch(index):
            self.fetched.append(index)
            return pages[index], total
        return [page async for page in http_client.pages(fetch, page_size, max_pages)]

    async def test_a_short_page_is_the_last(self):
        self.assertEqual(await self.collect([[1, 2], [3]]), [[1, 2], [3]])
        self.assertEqual(self.fetched, [0, 1])

    async def test_an_empty_page_is_the_last(self):
        self.assertEqual(await self.collect([[1, 2], []]), [[1, 2], []])

    async def test_stops_at_max_pages(self):
        await self.collect([[1, 2]] * 9, max_pages=3)
        self.assertEqual(self.fetched, [0, 1, 2])

    async def test_a_total_ends_on_a_full_page_without_another_request(self):
        self.assertEqual(await self.collect([[1, 2], [3, 4]], total=4), [[1, 2], [3, 4]])
        self.assertEqual(self.fetched, [0, 1])

    async def test_a_total_beats_a_short_page(self):
        await self.collect([[1], [2], [3]], total=6)
        self.assertEqual(self.fetched, [0, 1, 2])

    async def test_a_total_of_zero_stops_after_one_page(self):
        await self.collect([[], []], total=0)
        self.assertEqual(self.fetched, [0])

    async def test_breaking_out_fetches_no_more(self):
        fetched = []
        async def fetch(index):
            fetched.append(index)
            return [1, 2], None
        async for _ in http_client.pages(fetch, 2, 5):
            break
        self.assertEqual(fetched, [0])

if __name__ == "__main__":
    unittest.main()
