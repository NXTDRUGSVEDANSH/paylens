"""
Self-healing SQL. Ported from Fractal AI's intelligence/nlq/self_healing.py with
three security changes, each closing a real hole in the original.

1. HEALS THE PRE-RBAC TEMPLATE, NOT THE EXECUTED SQL.
   Fractal applies `_secure_sql` once (wrapping the query with a tenant predicate
   and a LIMIT), then hands that wrapped string to the healer and re-executes the
   result. Deleting the wrapper is a *plausible repair* for the most likely error
   ("column client_id does not exist"), so a correct-looking fix produces a
   cross-tenant leak. Healing the template removes the capability rather than
   trying to detect its misuse.

2. EVERY HEALED CANDIDATE IS RE-VALIDATED.
   `validate` is a required argument, not an option. A guard rejection consumes an
   attempt and is fed back as the next error message — the guard's reason is
   high-quality repair signal, for free.

3. NO `WHERE 1=0` ESCAPE HATCH.
   Fractal's prompt instructs: "If the required data does not exist, return a SELECT
   that produces 0 rows (e.g. WHERE 1=0)." That converts an unresolvable error into
   a valid, guard-passable, zero-row query which is then narrated as a factual
   "there were no failed transactions". For a payments tool a manufactured false
   negative with full evidence dressing is worse than an error, so an unresolvable
   query raises HealExhausted and is reported as a failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from backend import config
from backend.llm import provider
from backend.llm.provider import CallBudget

log = logging.getLogger("paylens.healing")

_SYSTEM = (
    "You are a DuckDB SQL expert fixing a broken analytics query. "
    "Return ONLY the corrected SQL — no explanation, no markdown fences, no JSON."
)

_PROMPT = """The following DuckDB query failed.

## Available tables (use ONLY these)
{tables}

## Semantic model
{schema_card}

## Original question
{question}

## Broken SQL
```sql
{sql}
```

## Error
{error}

Fix the SQL so it runs, using ONLY the tables and columns above.

Rules:
- Return a single SELECT statement.
- Do NOT add LIMIT, and do NOT add any merchant, region or tenant filter.
  Those are applied by the system after you return.
- Do NOT return placeholder SQL such as `SELECT 0`, `WHERE 1=0`, or anything
  designed to return zero rows. If the query cannot be fixed with the available
  columns, return your best genuine attempt at the intended analysis.
"""


class HealExhausted(RuntimeError):
    """All repair attempts failed. Reported as a failure, never as 'no data'."""

    def __init__(self, attempts: int, last_error: str):
        self.attempts, self.last_error = attempts, last_error
        super().__init__(f"SQL could not be repaired after {attempts} attempt(s): {last_error}")


@dataclass
class HealOutcome:
    sql: str                       # final BOUND, guarded SQL that executed
    template_sql: str              # final pre-RBAC template
    rows: list[dict]
    attempts: int = 0              # number of LLM repair calls actually made
    healed: bool = False
    errors: list[str] = field(default_factory=list)
    guard_first_try: bool = True


def heal_sql(template_sql: str, error: str, question: str, *, tables: list[str],
             schema_card: str, budget: CallBudget | None = None) -> str:
    """Ask the planner model to repair a template. Never sees an RBAC predicate."""
    raw = provider.call_text(
        _PROMPT.format(tables=", ".join(tables), schema_card=schema_card,
                       question=question, sql=template_sql, error=error),
        system=_SYSTEM, model=config.PLANNER_MODEL, purpose="heal", budget=budget)
    return provider.extract_sql(raw)


def run_with_healing(
    template_sql: str,
    *,
    question: str,
    validate: Callable[[str], tuple[bool, str, str]],
    execute: Callable[[str], list[dict]],
    tables: list[str],
    schema_card: str,
    max_attempts: int = None,
    budget: CallBudget | None = None,
    can_heal: bool = True,
) -> HealOutcome:
    """
    Validate -> execute -> on failure repair and repeat.

    `validate(template) -> (ok, reason, guarded_sql)` must be the real guard. Both
    a guard rejection and an execution error consume an attempt and are fed back as
    the error message.
    """
    max_attempts = config.MAX_SQL_RETRIES if max_attempts is None else max_attempts
    current = template_sql
    errors: list[str] = []
    guard_first_try = True

    for attempt in range(max_attempts + 1):
        ok, reason, guarded = validate(current)
        if not ok:
            if attempt == 0:
                guard_first_try = False
            errors.append(f"guard: {reason}")
            if attempt >= max_attempts or not can_heal or not provider.available():
                raise HealExhausted(attempt, reason)
            log.info("guard rejected (attempt %d): %s", attempt + 1, reason)
            current = heal_sql(current, reason, question, tables=tables,
                               schema_card=schema_card, budget=budget)
            continue
        try:
            rows = execute(guarded)
            return HealOutcome(sql=guarded, template_sql=current, rows=rows,
                               attempts=attempt, healed=attempt > 0, errors=errors,
                               guard_first_try=guard_first_try)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            errors.append(f"execute: {msg}")
            if attempt >= max_attempts or not can_heal or not provider.available():
                raise HealExhausted(attempt + 1, msg) from exc
            log.info("execution failed (attempt %d): %s", attempt + 1, msg)
            current = heal_sql(current, msg, question, tables=tables,
                               schema_card=schema_card, budget=budget)

    raise HealExhausted(max_attempts, errors[-1] if errors else "unknown")
