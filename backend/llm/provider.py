"""
LLM transport. Ported from Fractal AI's intelligence/nlq/llm.py — quota backoff,
<think>-block stripping (Qwen3 emits chain-of-thought), and tolerant JSON parsing.

Two models, one key:
    PLANNER_MODEL   qwen/qwen3-coder-next          plan + SQL + chart metadata + healing
    NARRATOR_MODEL  google/gemini-3-flash-preview  narration
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from backend import config

log = logging.getLogger("paylens.llm")


class LLMUnavailable(RuntimeError):
    """No API key, or the provider failed after retries."""


@dataclass
class CallRecord:
    purpose: str
    model: str
    ms: int
    ok: bool
    error: str | None = None


@dataclass
class CallBudget:
    """
    Hard per-request cap on LLM calls. Four engines means four places that could
    call a model; without an explicit budget an unlucky request fires six times
    and nobody notices. Every call is recorded so "what did that answer cost?"
    has an auditable answer.
    """
    limits: dict[str, int] = field(default_factory=lambda: {
        "plan": config.MAX_PLANNER_CALLS,
        "heal": config.MAX_HEAL_CALLS,
        "narrate": config.MAX_NARRATOR_CALLS,
        "actions": 1,
        "resolve": 1,
    })
    used: dict[str, int] = field(default_factory=dict)
    records: list[CallRecord] = field(default_factory=list)

    def spend(self, purpose: str) -> None:
        n = self.used.get(purpose, 0)
        cap = self.limits.get(purpose, 1)
        if n >= cap:
            raise LLMUnavailable(f"LLM call budget exhausted for '{purpose}' (cap {cap})")
        self.used[purpose] = n + 1

    @property
    def total(self) -> int:
        return sum(self.used.values())


def _client():
    if not config.LLM_API_KEY:
        raise LLMUnavailable("LLM_API_KEY is not set — copy .env.example to .env")
    from openai import OpenAI
    return OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)


def _is_quota_error(exc: Exception) -> bool:
    m = str(exc).lower()
    return any(k in m for k in ("429", "quota", "rate limit", "resource_exhausted"))


def _retry_delay(exc: Exception, fallback: float) -> float:
    m = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+(?:\.\d+)?)", str(exc))
    return float(m.group(1)) + 1.0 if m else fallback


def strip_reasoning(raw: str) -> tuple[str, str]:
    """
    Split Qwen3 <think> blocks off the answer. Returns (thinking, cleaned).

    A TRUNCATED block (opened, never closed) means the model ran out of tokens
    mid-thought and produced no answer — we return empty content so the caller
    treats it as a failure rather than parsing half a thought as SQL.
    """
    think = "\n".join(re.findall(r"<think>(.*?)</think>", raw, re.DOTALL)).strip()
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    if "<think>" in cleaned:
        return cleaned.split("<think>", 1)[1].strip(), ""
    return think, cleaned.strip()


def call_text(prompt: str, *, system: str | None = None, model: str | None = None,
              purpose: str = "plan", budget: CallBudget | None = None,
              temperature: float | None = None, max_retries: int = 3) -> str:
    model = model or config.PLANNER_MODEL
    if budget is not None:
        budget.spend(purpose)
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    t0 = time.time()
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = _client().chat.completions.create(
                model=model, messages=messages,
                temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
                max_tokens=config.LLM_MAX_TOKENS, timeout=config.LLM_TIMEOUT_S)
            raw = (resp.choices[0].message.content or "").strip()
            _, cleaned = strip_reasoning(raw)
            if not cleaned:
                raise LLMUnavailable("empty content (truncated <think> block?)")
            if budget is not None:
                budget.records.append(CallRecord(purpose, model, int((time.time() - t0) * 1000), True))
            return cleaned
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < max_retries - 1 and (_is_quota_error(exc) or "empty content" in str(exc)):
                wait = _retry_delay(exc, 5.0 * (2 ** attempt))
                log.warning("LLM retry %d/%d in %.1fs: %s", attempt + 1, max_retries, wait, exc)
                time.sleep(wait)
                continue
            break
    if budget is not None:
        budget.records.append(
            CallRecord(purpose, model, int((time.time() - t0) * 1000), False, str(last)))
    raise LLMUnavailable(f"{model} failed after {max_retries} attempts: {last}")


_FENCE = re.compile(r"```(?:json|sql)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict:
    """
    Pull one JSON object out of a model response.

    Fractal AI's `_parse_output` uses r"\\{[^{}]*\\}", which cannot match a nested
    object — so any response containing a sub-object silently failed to parse.
    This does brace matching instead, so nesting works.
    """
    for block in _FENCE.findall(text):
        block = block.strip()
        if block.startswith("{"):
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object found in response: {text[:300]!r}")


def extract_sql(text: str) -> str:
    """Pull a single SQL statement out of a model response."""
    for block in _FENCE.findall(text):
        b = block.strip()
        if re.match(r"^\s*(with|select)\b", b, re.I):
            return b.rstrip(";").strip()
    m = re.search(r"\b(with|select)\b.*", text, re.I | re.DOTALL)
    if m:
        return m.group(0).rstrip(";").strip()
    raise ValueError(f"no SQL found in response: {text[:300]!r}")


def call_json(prompt: str, *, system: str | None = None, model: str | None = None,
              purpose: str = "plan", budget: CallBudget | None = None,
              temperature: float | None = None) -> dict:
    return extract_json(call_text(prompt, system=system, model=model, purpose=purpose,
                                  budget=budget, temperature=temperature))


def available() -> bool:
    return bool(config.LLM_API_KEY)
