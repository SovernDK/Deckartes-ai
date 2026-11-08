"""Managing Ollama's models: which are installed, and releasing them from memory."""

from ollama_client import get_async_client

async def fetch_models() -> list[str]:
    """Return the names of locally available models, or [] on failure."""
    try:
        response = await get_async_client().list()
    except Exception:
        return []

    return [model.model for model in response.models]

async def unload_models() -> tuple[list[str], str | None]:
    """Release every model Ollama has loaded in memory. Returns (unloaded names, error or None)."""
    unloaded = []
    try:
        running = await get_async_client().ps()
        for model in running.models:
            # An empty request with keep_alive=0 makes Ollama unload the model right away
            await get_async_client().generate(model=model.model, keep_alive=0)
            unloaded.append(model.model)
    except Exception as e:
        return unloaded, str(e)
    return unloaded, None
