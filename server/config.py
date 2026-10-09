import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = Path(_env("DATA_DIR", "/data"))
WEB_DIR = Path(_env("WEB_DIR", str(Path(__file__).resolve().parent.parent / "web" / "dist")))

LLM_URL = _env("LLM_URL", "http://llm:8080/v1").rstrip("/")
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_MODEL = _env("LLM_MODEL", "local")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.2"))
LLM_MAX_STEPS = int(_env("LLM_MAX_STEPS", "10"))

WHISPER_MODEL = _env("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = _env("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE = _env("WHISPER_COMPUTE_TYPE", "float16")
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "en") or None

# A pause this long ends a speech segment; segments are force-cut at MAX_SEGMENT_S.
SILENCE_MS = int(_env("SILENCE_MS", "800"))
MAX_SEGMENT_S = float(_env("MAX_SEGMENT_S", "25"))

PORT = int(_env("PORT", "8443"))
# Extra hostnames/IPs (e.g. your LAN IP) to put in the self-signed certificate.
TLS_HOSTS = [h.strip() for h in _env("TLS_HOSTS", "").split(",") if h.strip()]

# Context budgets for the scribe (approximate tokens). Older sections collapse to summaries past the
# note budget; older conversation turns are summarised past the history budget.
NOTE_TOKEN_BUDGET = int(_env("NOTE_TOKEN_BUDGET", "20000"))
HISTORY_TOKEN_BUDGET = int(_env("HISTORY_TOKEN_BUDGET", "10000"))
KEEP_RECENT_TURNS = int(_env("KEEP_RECENT_TURNS", "15"))
# How many recent commits the scribe sees each turn, so it knows what ask_history can undo.
RECENT_COMMITS_IN_CONTEXT = int(_env("RECENT_COMMITS_IN_CONTEXT", "10"))
