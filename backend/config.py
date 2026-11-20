"""Central configuration. Env-driven, with safe defaults."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ── LLM (OpenRouter, OpenAI-compatible — mirrors Fractal AI's single-key setup) ──
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "qwen/qwen3-coder-next")
NARRATOR_MODEL = os.getenv("NARRATOR_MODEL", "google/gemini-3-flash-preview")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "90"))

# ── Storage ──
DUCKDB_PATH = Path(os.getenv("DUCKDB_PATH", ROOT / "data" / "paylens.duckdb")).expanduser()
if not DUCKDB_PATH.is_absolute():
    DUCKDB_PATH = (ROOT / DUCKDB_PATH).resolve()
# Absolute by construction. Fractal AI's CWD-relative "./chroma_db" puts the KB
# wherever uvicorn happened to be launched from.
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", ROOT / "data" / "chroma_db")).expanduser()
if not CHROMA_PATH.is_absolute():
    CHROMA_PATH = (ROOT / CHROMA_PATH).resolve()
SEMANTIC_MODEL_PATH = ROOT / "semantic" / "semantic_model.yaml"
SEEDS_PATH = ROOT / "semantic" / "seeds.yaml"

# ── Safety / cost controls ──
MAX_SQL_RETRIES = int(os.getenv("MAX_SQL_RETRIES", "2"))
MAX_SQL_ROWS = int(os.getenv("MAX_SQL_ROWS", "5000"))
EXPLAIN_SCAN_LIMIT = int(os.getenv("EXPLAIN_SCAN_LIMIT", "50000000"))
SQL_TIMEOUT_S = int(os.getenv("SQL_TIMEOUT_S", "10"))

# ── Caching ──
ANSWER_CACHE_TTL = int(os.getenv("ANSWER_CACHE_TTL", "600"))
ANSWER_CACHE_SIZE = int(os.getenv("ANSWER_CACHE_SIZE", "256"))
CACHE_T1 = float(os.getenv("CACHE_T1", "0.95"))     # reuse SQL, no planner call
CACHE_T2 = float(os.getenv("CACHE_T2", "0.75"))     # inject as few-shot
LEXICAL_FLOOR = float(os.getenv("LEXICAL_FLOOR", "0.60"))
PROMPT_VERSION = "plan-v1"

# ── LLM call budget, enforced by the orchestrator (not left emergent) ──
MAX_PLANNER_CALLS = 1
MAX_HEAL_CALLS = MAX_SQL_RETRIES
MAX_NARRATOR_CALLS = 1

# "Today" for the demo. The generated data ends here.
DEMO_NOW = os.getenv("DEMO_NOW", "2026-11-20T12:00:00")
