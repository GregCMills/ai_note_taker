import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() not in ("0", "false", "no", "off", "")


DATA_DIR = Path(_env("DATA_DIR", "/data"))
WEB_DIR = Path(_env("WEB_DIR", str(Path(__file__).resolve().parent.parent / "web" / "dist")))

LLM_URL = _env("LLM_URL", "http://llm:8080/v1").rstrip("/")
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_MODEL = _env("LLM_MODEL", "local")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.2"))
LLM_MAX_STEPS = int(_env("LLM_MAX_STEPS", "10"))
# Caps on generated tokens, so a model stuck repeating itself can't run until the context is full (it once
# generated 17,000 tokens in one request). Per request, and in total across one agent turn.
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "4096"))
LLM_TURN_OUTPUT_TOKENS = int(_env("LLM_TURN_OUTPUT_TOKENS", "10000"))
LIBRARIAN_MAX_TOKENS = int(_env("LIBRARIAN_MAX_TOKENS", "600"))
# The llama-server context size (-c in docker-compose.gemma.yml / docker-compose.qwen.yml); shown in the agent inspector.
LLM_CONTEXT = int(_env("LLM_CONTEXT", "40960"))

# Speech-to-text engine: "granite" (IBM Granite Speech, more accurate), "whisper" (faster-whisper), or "api" (any
# OpenAI-compatible /audio/transcriptions endpoint at STT_URL, so speech-to-text can run on another machine or service).
ASR_ENGINE = _env("ASR_ENGINE", "whisper")
STT_URL = _env("STT_URL", "").rstrip("/")
STT_API_KEY = _env("STT_API_KEY", "")
STT_MODEL = _env("STT_MODEL", "whisper-1")
GRANITE_MODEL = _env("GRANITE_MODEL", "ibm-granite/granite-speech-4.1-2b")
WHISPER_MODEL = _env("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = _env("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE = _env("WHISPER_COMPUTE_TYPE", "float16")
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "en") or None

# Show words while a sentence is still being spoken, by transcribing it again about once a second. A hosted
# speech-to-text API bills each of those calls, so docker-compose.api.yml turns this off.
LIVE_TRANSCRIPT = _flag("LIVE_TRANSCRIPT", True)
# After each turn, have the LLM read the next turn's prompt in advance so it starts faster (llama.cpp's prompt cache).
# A hosted LLM bills that as an extra full request, so docker-compose.api.yml turns this off.
LLM_WARMUP = _flag("LLM_WARMUP", True)

# A pause this long ends a speech segment; segments are force-cut at MAX_SEGMENT_S.
SILENCE_MS = int(_env("SILENCE_MS", "800"))
MAX_SEGMENT_S = float(_env("MAX_SEGMENT_S", "25"))

PORT = int(_env("PORT", "8443"))
# Extra hostnames/IPs (e.g. your LAN IP) to put in the self-signed certificate.
TLS_HOSTS = [h.strip() for h in _env("TLS_HOSTS", "").split(",") if h.strip()]

# Context budgets for the scribe (approximate tokens). Older sections collapse to summaries past the
# note budget; older conversation turns are summarised past the history budget.
NOTE_TOKEN_BUDGET = int(_env("NOTE_TOKEN_BUDGET", "20000"))
# When a long section has to be summarised, this much of its end stays word for word.
KEEP_TAIL_TOKENS = int(_env("KEEP_TAIL_TOKENS", "1500"))
# Recent turns come after the notes in the prompt, so they are re-read every turn: keep them short.
HISTORY_TOKEN_BUDGET = int(_env("HISTORY_TOKEN_BUDGET", "4000"))
KEEP_RECENT_TURNS = int(_env("KEEP_RECENT_TURNS", "8"))
# How many recent commits the scribe sees each turn, so it knows what ask_history can undo.
RECENT_COMMITS_IN_CONTEXT = int(_env("RECENT_COMMITS_IN_CONTEXT", "10"))
