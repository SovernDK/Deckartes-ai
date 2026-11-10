"""Web search and page fetching through Ollama's web API (https://docs.ollama.com/capabilities/web-search).

Needs an API key from https://ollama.com/settings/keys in config.ollama_api_key
(read from the OLLAMA_API_KEY environment variable or the .env file).
"""

from core import http_client
from core.config import config

API_URL = "https://ollama.com/api"
MAX_RESULTS = 5
# Caps on web text handed to the model, so results can't fill its context window
MAX_PAGE_CHARS = 8000
MAX_SNIPPET_CHARS = 1500
MAX_LINKS = 20

class WebSearchError(Exception):
    pass

async def _post(endpoint: str, payload: dict) -> dict:
    """POST to Ollama's web API; raise WebSearchError on any failure."""
    res = await http_client.request("POST", f"{API_URL}/{endpoint}", name="Ollama web API", error=WebSearchError,
                                    json=payload, token=config.ollama_api_key, token_env="OLLAMA_API_KEY")
    return res.json()

async def web_search(query: str) -> list[dict]:
    """Search the web; returns up to MAX_RESULTS of {title, url, content}, long snippets cut."""
    data = await _post("web_search", {"query": query, "max_results": MAX_RESULTS})
    results = data.get("results", [])
    for result in results:
        content = result.get("content") or ""
        if len(content) > MAX_SNIPPET_CHARS:
            result["content"] = content[:MAX_SNIPPET_CHARS] + " [...]"
    return results

async def web_fetch(url: str) -> dict:
    """Fetch a web page; returns {title, content, links}, long text and link lists cut."""
    page = await _post("web_fetch", {"url": url})
    content = page.get("content") or ""
    if len(content) > MAX_PAGE_CHARS:
        page["content"] = content[:MAX_PAGE_CHARS] + "\n[...page truncated]"
    page["links"] = (page.get("links") or [])[:MAX_LINKS]
    return page
