"""A fake server behind http_client._transport, shared by the tests that go through HTTP."""

import unittest
from unittest import mock

import httpx

import http_client

class FakeServer:
    """Answers requests from a {path: response} table and records them; a response can be
    an httpx.Response, a JSON-able value, a str (text/markdown), an exception to raise,
    or a function of the request returning any of those."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes[request.url.path]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, str):
            return httpx.Response(200, text=answer)
        return httpx.Response(200, json=answer)

class HttpTest(unittest.IsolatedAsyncioTestCase):
    def serve(self, routes: dict) -> FakeServer:
        server = FakeServer(routes)
        http_client._cache.clear()
        self.addCleanup(http_client._cache.clear)
        patcher = mock.patch.object(http_client, "_transport", httpx.MockTransport(server))
        patcher.start()
        self.addCleanup(patcher.stop)
        return server
