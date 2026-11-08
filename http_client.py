"""HTTP for the tool backends: one request function with an optional cache and error messages
a model can pass on.

Every failure raises the backend's own exception class with a readable message (unreachable,
missing token, rate limit, other status), which tools.run_tool hands to the model.
Responses are cached in memory for max_age seconds; failures never are.
"""

import datetime as dt
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

# Tests set an httpx.MockTransport here in place of the network
_transport: httpx.AsyncBaseTransport | None = None

_cache: dict = {}

async def request(method: str, url: str, *, name: str, error: type[Exception],
                  params: dict | None = None, json: dict | None = None,
                  token: str | None = None, token_env: str = "", max_age: float = 0,
                  timeout: float = 30, status_messages: dict[int, str] | None = None) -> httpx.Response:
    """Send a request and return the 200 response, cached for max_age seconds (0 = not cached).

    name is how messages call the service ("Could not reach {name}"). A token is sent as a
    Bearer header; token="" (not set) raises at once, naming token_env. status_messages
    replaces the message for given statuses, e.g. a 403 that means "not in your plan".
    """
    if token is not None and not token:
        raise error(f"No {name} token: set {token_env} in the environment or .env")

    params = params or {}
    key = (method, url, tuple(sorted(params.items())), repr(json))
    cached = _cache.get(key)
    if max_age and cached and time.monotonic() - cached[0] < max_age:
        return cached[1]

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=_transport) as client:
            res = await client.request(method, url, params=params, json=json, headers=headers)
    except httpx.HTTPError as e:
        raise error(f"Could not reach {name}: {e}") from e

    if res.status_code in (status_messages or {}):
        raise error(status_messages[res.status_code])
    if res.status_code == 429:
        reset = res.headers.get("x-ratelimit-reset")
        when = f"resets at {dt.datetime.fromtimestamp(int(reset)):%H:%M:%S}" if reset else "try again later"
        raise error(f"{name} rate limit reached, {when}")
    if res.status_code != 200:
        raise error(f"{name} responded with status {res.status_code}: {res.text[:200]}")

    if max_age:
        _cache[key] = (time.monotonic(), res)
    return res

async def pages(fetch: Callable[[int], Awaitable[tuple[list, int | None]]], page_size: int,
                max_pages: int) -> AsyncIterator[list]:
    """Yield a list's pages, stopping after the last one or max_pages. Break out to stop early.

    await fetch(index), with index counting from 0, returns (items, total). A total (the API's
    count of all items) says when the pages run out; with None, a page under page_size is the last.
    """
    for index in range(max_pages):
        items, total = await fetch(index)
        yield items
        if (index + 1) * page_size >= total if total is not None else len(items) < page_size:
            return
