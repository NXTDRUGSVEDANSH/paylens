"""
Two answer caches, both exact-hash (no embedding, so both are genuinely fast).

  L0  question_key(question, role, watermark)   ~1ms   skips the ENTIRE pipeline
  L2  answer_key(post_rbac_sql, role, watermark) ~1ms  skips execute/DQ/drivers/narrate

The semantic Q->SQL cache (kb/retrieve.py) is a different layer: it skips the PLANNER
call but still pays ~200ms to embed the question, so it cannot deliver a sub-100ms
repeat on its own. L0 is what makes an identical repeat instant; L1 is what helps a
REPHRASED question, which L0 cannot.

The L2 key derives from the POST-RBAC SQL, which textually contains the scope
predicate — so serving one role's result to another is physically impossible rather
than merely prevented.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from typing import Any

from backend import config

_store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()


def _hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _common(role_persona_key: str, watermark: str) -> tuple:
    return (config.PROMPT_VERSION, config.MAX_SQL_ROWS, config.PLANNER_MODEL,
            config.NARRATOR_MODEL, role_persona_key, watermark)


def question_key(question: str, role: str, watermark: str) -> str:
    """L0. Normalised whitespace/case so trivial retyping still hits."""
    norm = re.sub(r"\s+", " ", question.strip().lower()).rstrip("?.! ")
    return "q:" + _hash("pl-v1", norm, *_common(role, watermark))


def answer_key(post_rbac_sql: str, role: str, watermark: str) -> str:
    """L2. Keyed on the executed SQL, so the row scope is already inside the key."""
    norm = re.sub(r"\s+", " ", post_rbac_sql.strip())
    return "a:" + _hash("pl-v1", norm, *_common(role, watermark))


def get(key: str) -> Any | None:
    hit = _store.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > config.ANSWER_CACHE_TTL:
        _store.pop(key, None)
        return None
    _store.move_to_end(key)
    return value


def put(key: str, value: Any) -> None:
    _store[key] = (time.time(), value)
    _store.move_to_end(key)
    while len(_store) > config.ANSWER_CACHE_SIZE:
        _store.popitem(last=False)


def invalidate(prefix: str | None = None) -> int:
    keys = [k for k in _store if prefix is None or k.startswith(prefix)]
    for k in keys:
        _store.pop(k, None)
    return len(keys)


def stats() -> dict:
    return {"entries": len(_store), "capacity": config.ANSWER_CACHE_SIZE,
            "ttl_s": config.ANSWER_CACHE_TTL}
