"""Web search and page fetching through Ollama's web API (https://docs.ollama.com/capabilities/web-search).

Needs an API key from https://ollama.com/settings/keys in config.ollama_api_key
(read from the OLLAMA_API_KEY environment variable or the .env file).
"""

import httpx

from config import config

API_URL = "https://ollama.com/api"

class WebSearchError(Exception):
    pass

async def _post(endpoint: str, payload: dict) -> dict:
    """POST to Ollama's web API; raise WebSearchError on any failure."""
    if not config.ollama_api_key:
        raise WebSearchError("No Ollama API key: set OLLAMA_API_KEY in the environment or .env")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{API_URL}/{endpoint}",
                json=payload,
                headers={"Authorization": f"Bearer {config.ollama_api_key}"},
            )
    except httpx.HTTPError as e:
        raise WebSearchError(f"Could not reach the Ollama web API: {e}") from e

    if res.status_code != 200:
        raise WebSearchError(f"Ollama web API responded with status {res.status_code}: {res.text[:200]}")

    return res.json()

async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web; returns up to max_results (1-10) of {title, url, content}."""
    max_results = max(1, min(10, max_results))
    data = await _post("web_search", {"query": query, "max_results": max_results})
    return data.get("results", [])

async def web_fetch(url: str) -> dict:
    """Fetch a web page; returns {title, content, links}."""
    return await _post("web_fetch", {"url": url})
