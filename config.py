import os
from dataclasses import dataclass, field
from pathlib import Path

def read_env(name: str) -> str:
    """Value from the environment, else from the .env file next to this script, else ""."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = Path(__file__).with_name(".env")
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
    host: str = "http://localhost:11434"
    model: str = "granite4.1:3b"
    # One of CONTEXT_LENGTHS
    context_length: int = 16384
    # None = the model's own default
    temperature: float | None = None
    # Send only the tool groups a question needs, picked by a small embedding model (tool_router.py)
    tool_routing: bool = True
    embed_model: str = "all-minilm:33m"
    # Embeds chunks of a long loaded file (rag.py); multilingual, unlike embed_model
    rag_embed_model: str = "embeddinggemma-2:570m"
    # Show tokens used per reply and for the whole conversation (key t)
    show_token_usage: bool = False
    # Name of a preset in prompts.json
    system_prompt: str = "Assistant"
    glances_host: str = "http://thinkpad:61208"
    ollama_api_key: str = field(default_factory=lambda: read_env("OLLAMA_API_KEY"), repr=False)
    animeschedule_token: str = field(default_factory=lambda: read_env("ANIMESCHEDULE_TOKEN"), repr=False)
    pandascore_token: str = field(default_factory=lambda: read_env("PANDA_SCORE_API_TOKEN"), repr=False)
    # Lists of all CS matches/events keep only this PandaScore tier or better (s, a, b, c, d)
    cs_min_tier: str = "b"
    # Whose anime list the list tool reads (must be public)
    animeschedule_user: str = "SeggyX"
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
