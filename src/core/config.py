import os
from dataclasses import dataclass, field
from pathlib import Path

# The repo folder, which holds .env and the data files; this file is src/core/config.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]

def read_env(name: str) -> str:
    """Value from the environment, else from the .env file in the repo folder, else ""."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"\'')
    return ""

def local_timezone() -> str:
    """IANA name of this machine's timezone (e.g. Europe/Warsaw), from TZ or /etc/localtime."""
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    return "Europe/London"

# Context windows to choose from in the settings popup (key c)
CONTEXT_LENGTHS = [2048, 4096, 8192, 16384, 32768]

@dataclass
class Config:
    # Rapid-MLX chat server; the app starts it (mlx_server.py)
    host: str = "http://127.0.0.1:8000"
    # Embeddings-only server for the library, because one server holds one embedding model
    file_embed_host: str = "http://127.0.0.1:8001"
    # The files the user saved and their embeddings (library.py); not in ~/.cache, because it can't be rebuilt without them
    library_path: Path = Path.home() / ".local" / "share" / "deckartes" / "library.db"
    mlx_server_command: str = "~/.local/bin/rapid-mlx"
    model: str = "LiquidAI/LFM3-1.7B-MLX-4bit"
    # One of CONTEXT_LENGTHS. The app's own budget (when to compact); the server doesn't cut the prompt to it
    context_length: int = 16384
    # Longest reply in tokens, thinking included
    max_tokens: int = 8192
    # The model reasons before each chat answer (not for the compaction summary). Its thinking is
    # only shown, in the app's Detailed mode; it's never saved or sent back to the model
    enable_thinking: bool = True
    # Summarise the conversation into one message once a turn's prompt plus reply reaches this
    # share of context_length (0.6 = 60%), so it stays clear of the window's limit and the
    # model's quality drop near it. None = never compact.
    compact_at: float | None = 0.6
    # Sampling; None = the model's own default. These are LFM3's recommended chat and tool settings
    temperature: float | None = 0.1
    top_k: int | None = 50
    repetition_penalty: float | None = 1.1
    # Send only the tool groups a question needs, picked by a small embedding model (tool_router.py)
    tool_routing: bool = True
    embed_model: str = "mlx-community/all-MiniLM-L6-v2-4bit"
    # Embeds the library's chunks and searches (library.py); multilingual, unlike embed_model
    rag_embed_model: str = "mlx-community/embeddinggemma-2-4bit"
    # Detailed mode (key shift+t): show the model's thinking and the tokens used; off = Default mode
    detailed: bool = False
    # Name of a preset in prompts.json
    system_prompt: str = "Assistant"
    glances_host: str = "http://thinkpad:61208"
    ollama_api_key: str = field(default_factory=lambda: read_env("OLLAMA_API_KEY"), repr=False)
    animeschedule_token: str = field(default_factory=lambda: read_env("ANIMESCHEDULE_TOKEN"), repr=False)
    pandascore_token: str = field(default_factory=lambda: read_env("PANDA_SCORE_API_TOKEN"), repr=False)
    telegram_token: str = field(default_factory=lambda: read_env("TELEGRAM_BOT_TOKEN"), repr=False)
    # The only Telegram user the bot answers; the bot won't start without it
    telegram_user_id: str = field(default_factory=lambda: read_env("TELEGRAM_USER_ID"), repr=False)
    # Lists of all CS matches/events keep only this PandaScore tier or better (s, a, b, c, d)
    cs_min_tier: str = "b"
    # Whose anime list the list tool reads (must be public)
    animeschedule_user: str = "SeggyX"
    # Which statuses on that list the morning digest covers (anime_digest.py)
    anime_digest_statuses: tuple[str, ...] = ("watching", "to-watch")
    # Calendar that add_calendar_event adds to; "" = the default calendar in Calendar's settings
    calendar_name: str = ""
    # Used for "today" and airing times
    timezone: str = field(default_factory=local_timezone)
    # Timetable to follow: "sub" (subtitled release), "raw" (Japanese broadcast) or "dub"
    anime_air_type: str = "sub"
    # Anime rated at or below this (0-10 scale, i.e. 6.0 = 60%) are left out; unrated ones are kept
    anime_min_rating: float = 6.0

# Shared instance: import it anywhere and read or change its fields
config = Config()
