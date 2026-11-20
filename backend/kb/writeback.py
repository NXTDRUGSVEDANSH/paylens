"""
KB write-back, gated.

Fractal AI stores the POST-`_secure_sql` string (`sql_rag.py` passes `final_sql`),
which bakes in `client_id = 7`, `LIMIT 500` and absolute dates. The LIMIT is the
nasty one: the stored example teaches the model to emit `LIMIT 500` itself, the guard
then sees a limit already present and declines to add its own, and the configured
row cap is silently replaced by 500 — a truncated answer that passes every check.

So we store the PRE-RBAC, PRE-LIMIT, PARAMETERISED template plus the plan.
"""
from __future__ import annotations

import logging
import time

from backend.core.resolver import Resolution
from backend.core.schemas import AnalysisPlan, CompiledQuery, RunTrace
from backend.kb import store
from backend.kb.keys import record_id
from backend.kb.store import KBRecord

log = logging.getLogger("paylens.kb.writeback")


def eligible(trace: RunTrace, *, row_count: int, has_block_caveat: bool,
             truncated: bool, is_detail: bool, scope_violations: int,
             plan_from_llm: bool) -> tuple[bool, str]:
    """
    All conditions must hold. The two judgement calls:

    - HEALED queries are quarantined, never promoted. Healing means generation #1 was
      wrong; "it ran" proves syntactic validity, not semantic correctness. Promoting
      it would also contradict the -0.25 confidence penalty we apply for healing.
    - ZERO ROWS is never stored, not even quarantined. It is the shared signature of
      a wrong join, an over-narrow filter, an empty window and the `WHERE 1=0` escape.
      A correctly-empty query is trivial to regenerate; a wrongly-empty one cached at
      tier 1 yields a permanently confident "there were none".
    """
    if not trace.guard_first_try:
        return False, "guard rejected the first attempt"
    if trace.healed:
        return False, "query required healing (quarantined, not promoted)"
    if row_count <= 0:
        return False, "zero rows — cannot distinguish correct-empty from wrong query"
    if has_block_caveat:
        return False, "a blocking data-quality caveat applies"
    if is_detail and truncated:
        return False, "truncated detail listing"
    if scope_violations:
        return False, "scope violation"
    if not plan_from_llm:
        return False, "plan came from cache or deterministic resolution (nothing new)"
    return True, "ok"


def write_back(res: Resolution, plan: AnalysisPlan, cq: CompiledQuery, *,
               model_version: str, prompt_version: str, trace: RunTrace,
               row_count: int, exec_ms: int, quarantine: bool = False,
               golden_id: str | None = None) -> str | None:
    if not store.available():
        return None
    rid = record_id(res.shape, res.canonical_question)
    meta = {
        "shape_key": res.shape,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "verified": not quarantine,
        "healed": bool(trace.healed),
        "source": "learned",
        "role_agnostic": True,          # invariant: template carries no role predicate
        "invalid_reason": "",
        "created_at": int(time.time()),
        "last_used_at": int(time.time()),
        "use_count": 0,
        "success_count": 1,
        "golden_id": golden_id or "",
        "intent": plan.intent,
        "metrics_csv": ",".join([plan.metric, *plan.secondary_metrics]),
        "dims_csv": ",".join(plan.segment_by),
        "filter_dims_csv": ",".join(f.dimension for f in plan.filters),
        "param_names_csv": ",".join(sorted(cq.params)),
        "param_arity": len(cq.params),
        "is_aggregate": cq.is_aggregate,
        "time_grain": plan.time_grain or "",
        "row_count": row_count,
        "exec_ms": exec_ms,
    }
    rec = KBRecord(
        record_id=rid, canonical_question=res.canonical_question,
        raw_question=res.question, template_sql=cq.template_sql,
        plan=plan.model_dump(mode="json"),
        param_schema={k: v.type for k, v in cq.params.items()}, meta=meta)
    store.upsert(rec)
    log.info("KB %s: %s", "quarantined" if quarantine else "learned",
             res.canonical_question[:80])
    return rid
