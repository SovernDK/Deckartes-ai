import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import tzlocal
from dotenv import dotenv_values

# The repo folder, which holds .env and the data files; this file is src/core/config.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Read without loading into os.environ, so child processes (the model servers) don't get the keys
ENV_FILE = dotenv_values(PROJECT_ROOT / ".env")

def read_env(name: str) -> str:
    return os.environ.get(name) or ENV_FILE.get(name) or ""

# When the system's time zone can't be found, or its settings conflict
FALLBACK_TIMEZONE = "UTC"

def local_timezone() -> str:
    try:
        return tzlocal.get_localzone_name() or FALLBACK_TIMEZONE
    except ZoneInfoNotFoundError:
        return FALLBACK_TIMEZONE

CONTEXT_LENGTHS = [2048, 4096, 8192, 16384, 32768]

@dataclass
class Config:
    # Rapid-MLX chat server; the app starts it (mlx_server.py)
    host: str = "http://127.0.0.1:8000"
    # Embeddings-only server for the library, because one server holds one embedding model
    file_embed_host: str = "http://127.0.0.1:8001"
    # The files the user saved and their embeddings (library.py); not in ~/.cache, because it can't be rebuilt without them
    library_path: Path = Path.home() / ".local" / "share" / "deckartes" / "library.db"
    history_path: Path = Path.home() / ".local" / "share" / "deckartes" / "history.db"
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
    # Labels spending payees (spending.py): a decision model, which scores every category rather than
    # writing an answer. It runs on its own Rapid-MLX server, started only while labeling
    decision_host: str = "http://127.0.0.1:8002"
    # Decider-2b quantized to 4 bits, made by model/quantize_decider.py (docs/model-server.md)
    decision_model: Path = Path.home() / ".local" / "share" / "deckartes" / "models" / "decider-2b-4bit"
    # Detailed mode (key shift+t): show the model's thinking and the tokens used; off = Default mode
    detailed: bool = False
    # Name of a preset in prompts.json
    system_prompt: str = "Assistant"
    glances_host: str = "http://thinkpad:61208"
    ollama_api_key: str = field(default_factory=lambda: read_env("OLLAMA_API_KEY"), repr=False)
    animeschedule_token: str = field(default_factory=lambda: read_env("ANIMESCHEDULE_TOKEN"), repr=False)
    pandascore_token: str = field(default_factory=lambda: read_env("PANDA_SCORE_API_TOKEN"), repr=False)
    # Enable Banking production application id and the path of the private key (.pem) downloaded with it
    enable_banking_app_id: str = field(default_factory=lambda: read_env("ENABLE_BANKING_APP_ID"), repr=False)
    enable_banking_key_path: str = field(default_factory=lambda: read_env("ENABLE_BANKING_KEY_PATH"), repr=False)
    # The same for the sandbox application (made-up banks), used instead when enable_banking_sandbox is on
    enable_banking_sandbox_id: str = field(default_factory=lambda: read_env("ENABLE_BANKING_SANDBOX_ID"), repr=False)
    enable_banking_sandbox_key_path: str = field(default_factory=lambda: read_env("ENABLE_BANKING_SANDBOX_KEY_PATH"),
                                                 repr=False)
    # Test against the sandbox app and its own bank link: ENABLE_BANKING_SANDBOX=1, or link with --sandbox
    enable_banking_sandbox: bool = field(default_factory=lambda: read_env("ENABLE_BANKING_SANDBOX") == "1")
    # Must match a redirect URL registered for the Enable Banking application. Production apps need
    # https and not localhost: this page (pages/callback, on GitHub Pages) only shows the address
    # to paste back into the linking command
    enable_banking_redirect_url: str = "https://soverndk.github.io/Deckartes-ai/callback/"
    # The bank link: Enable Banking's session and accounts (enable_banking_api.py); not in ~/.cache, since linking needs the user
    banking_session_path: Path = Path.home() / ".local" / "share" / "deckartes" / "banking_session.json"
    # The sandbox app's bank link, kept apart so testing never replaces the real one
    banking_sandbox_session_path: Path = Path.home() / ".local" / "share" / "deckartes" / "banking_sandbox_session.json"
    # Transactions listed per account by get_bank_transactions; keeps a long range inside the
    # context window, while the totals still count every transaction
    bank_max_transactions: int = 100
    # Stored transactions of the last this many days are fetched again once they're older than
    # bank_fresh_minutes, since recent payments still change (pending ones book, sometimes on another day)
    bank_settle_days: int = 7
    bank_fresh_minutes: int = 15
    # Spending categories each payee is labeled with, and each finished week's and month's totals
    # (spending.py); not in ~/.cache, since past periods can't be fetched again
    spending_path: Path = Path.home() / ".local" / "share" / "deckartes" / "spending.db"
    # The sandbox app's, kept apart so made-up payments never mix with the real history
    spending_sandbox_path: Path = Path.home() / ".local" / "share" / "deckartes" / "spending_sandbox.db"
    # A payee's category is the decision model's best one only when it gives it at least this
    # probability; otherwise the payee stays undecided for the user to label. Fitted to the 4-bit model
    spending_label_min_probability: float = 0.5
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
    timezone: str = field(default_factory=local_timezone)
    # Timetable to follow: "sub" (subtitled release), "raw" (Japanese broadcast) or "dub"
    anime_air_type: str = "sub"
    # Anime rated at or below this (0-10 scale, i.e. 6.0 = 60%) are left out; unrated ones are kept
    anime_min_rating: float = 6.0

config = Config()
