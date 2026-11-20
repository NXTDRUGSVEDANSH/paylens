"""
The orchestrator. Owns the step order, the LLM call budget, and the invariants.

INVARIANTS (these are the contract; everything else is detail)
  1. Nothing reaches DuckDB that did not pass the guard on THIS request — no
     exception for cache hits, healed SQL, DQ probes, or the row-count query.
  2. The RBAC scope pre-check runs BEFORE any cache lookup, so a refusal cannot be
     bypassed by a cache hit.
  3. The KB stores role-free, LIMIT-free, parameterised SQL. Scope and caps are
     applied at guard time, every time.
  4. The answer-cache key derives from the post-RBAC SQL, so serving one role's
     result to another is impossible by construction.
  5. Data-quality caveats ride inside the cached answer, whose key contains the data
     watermark — they are never cached independently of the result.
  6. A guard failure on a tier-1 reuse is a CACHE MISS, not an error: fall through
     to a fresh plan and demote the stale record.
  7. The LLM never sees an RBAC predicate; healing operates on the pre-injection
     template.
  8. The LLM call budget is enforced here, not left to emerge.
"""
from __future__ import annotations

import functools
import logging
import time
from datetime import date

from backend import config
from backend.core import actions as action_engine
from backend.core import cache, chartmeta, confidence, db, dq, drivers, rbac
from backend.core.compiler import bind, compile_plan, to_count_query
from backend.core.executor import (QueryTimeout, RowCapExceeded, apply_row_cap, execute)
from backend.core.guard import GuardBlocked, guard
from backend.core.healing import HealExhausted, run_with_healing
from backend.core.narrator import narrate
from backend.core.planner import build_plan
from backend.core.resolver import Resolution, destructive_intent, resolve
from backend.core.schemas import (AnalysisPlan, AnswerCard, CacheTier, Caveat,
                                  Decomposition, Evidence, LLMCall, RunTrace,
                                  ScopeViolation, Segment, TimeWindow)
from backend.kb import retrieve as kb_retrieve
from backend.kb import store as kb_store
from backend.kb import writeback as kb_writeback
from backend.llm.provider import CallBudget
from semantic.loader import SemanticModel, get_model

log = logging.getLogger("paylens.pipeline")


@functools.lru_cache(maxsize=1)
def _known_entity_values() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"region": set(), "state": set()}
    try:
        for r in db.fetch_dicts("select distinct region, state from dim_merchant"):
            if r.get("region"):
                out["region"].add(r["region"])
            if r.get("state"):
                out["state"].add(r["state"])
    except Exception as exc:  # noqa: BLE001
        log.warning("entity preload failed: %s", exc)
    return out


class _Timer:
    def __init__(self, trace: RunTrace, name: str):
        self.trace, self.name = trace, name

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *a):
        self.trace.step_ms[self.name] = int((time.time() - self.t0) * 1000)


def _guarded_runner(p: rbac.Principal, model: SemanticModel):
    """Every helper query (DQ probes, row counts) goes through the guard. Invariant 1."""
    def run(sql: str) -> list[dict]:
        g = guard(sql, p, model, run_explain=False)
        return execute(g.sql).rows
    return run


def _segments(rows: list[dict], dim: str, metric_id: str, period: str | None
              ) -> list[Segment]:
    out: list[Segment] = []
    for r in rows:
        if period is not None and r.get("period") != period:
            continue
        key = r.get(dim)
        val = r.get(metric_id)
        den = r.get("_den")
        if key is None or val is None or not den:
            continue
        out.append(Segment(key=str(key), weight=float(den), value=float(val),
                           numerator=r.get("_num"), denominator=float(den)))
    return out


def _decompose_all(rows: list[dict], plan: AnalysisPlan, model: SemanticModel
                   ) -> list[Decomposition]:
    if not (plan.needs_decomposition and plan.baseline and plan.segment_by):
        return []
    metric = model.metric(plan.metric)
    if not metric.is_ratio or not metric.decomposable:
        return []
    out: list[Decomposition] = []
    for dim in plan.segment_by:
        base = _segments(rows, dim, plan.metric, "baseline")
        targ = _segments(rows, dim, plan.metric, "target")
        if not base or not targ:
            continue
        out.append(drivers.decompose(dim, base, targ))
    return drivers.rank_dimensions(out)


def _retriable_pct(run, window: TimeWindow) -> float | None:
    try:
        rows = run(f"""
            SELECT 100.0 * count(*) FILTER (WHERE fc.is_retriable) / nullif(count(*), 0) AS pct
            FROM fact_transactions t
            JOIN dim_failure_code fc ON fc.failure_code = t.failure_code
            WHERE t.status = 'FAILED' AND t.txn_ts >= '{window.start.isoformat()}'
              AND t.txn_ts < '{(window.end.toordinal() + 1) and window.end.isoformat()}'
        """)
        return float(rows[0]["pct"]) if rows and rows[0].get("pct") is not None else None
    except Exception:  # noqa: BLE001
        return None


def ask(question: str, *, role: str = "cxo", claims: dict | None = None,
        today: date | None = None, model: SemanticModel | None = None,
        use_cache: bool = True) -> AnswerCard:
    model = model or get_model()
    trace = RunTrace()
    budget = CallBudget()
    t_start = time.time()
    p = rbac.principal_for(role, claims or {})

    def finish(card: AnswerCard) -> AnswerCard:
        trace.total_ms = int((time.time() - t_start) * 1000)
        trace.llm_calls = [LLMCall(purpose=r.purpose, model=r.model, ms=r.ms, ok=r.ok)
                           for r in budget.records]
        card.trace = trace
        return card

    def refuse(reason: str, violations: list[ScopeViolation] | None = None) -> AnswerCard:
        return finish(AnswerCard(ok=False, question=question, role=role, refused=True,
                                 refusal_reason=reason,
                                 scope_violations=violations or []))

    # ── 0. destructive intent, refused before anything else ─────────────────
    # PayLens never executes user-supplied SQL, so injection is structurally
    # impossible. But a user asking to mutate data deserves a clear "read-only"
    # answer, not an analytics result about a keyword in their sentence.
    verb = destructive_intent(question)
    if verb:
        return refuse(
            f"PayLens is read-only and cannot {verb} anything. It answers analytical "
            f"questions over payments data; it has no write path to the database, and "
            f"every generated query is validated as SELECT-only before execution. "
            f"Try asking what you wanted to know about those transactions instead.")

    # ── 1. resolve (deterministic, no LLM) ───────────────────────────────────
    with _Timer(trace, "resolve"):
        res: Resolution = resolve(question, model, today)

    # ── 2. RBAC scope pre-check — BEFORE any cache lookup (invariant 2) ──────
    with _Timer(trace, "scope"):
        try:
            entities = rbac.named_entities(question, model, _known_entity_values())
            violations = rbac.scope_violations(p, entities, model)
        except PermissionError as exc:
            return refuse(str(exc))
        if violations:
            vs = [ScopeViolation(**v) for v in violations]
            names = ", ".join(v.entity for v in vs)
            return refuse(
                f"That question references {names}, which is outside your access scope. "
                "Returning a filtered answer would silently give you the wrong number, "
                "so the request is refused instead. " + (vs[0].allowed_alternative or ""),
                vs)
        if res.metrics and res.metrics[0] in rbac.denied_metrics(p, model):
            return refuse(f"The metric '{res.metrics[0]}' is not available to role "
                          f"'{role}'.")

    watermark = db.data_watermark()

    # ── 3. L0 answer cache: identical question, same role, same data ─────────
    q_key = cache.question_key(question, role, watermark)
    if use_cache:
        hit = cache.get(q_key)
        if hit is not None:
            card = hit.model_copy(deep=True)
            # Set the flag on the LOCAL trace: finish() replaces card.trace, so
            # mutating the copied card's trace would be silently discarded.
            trace.answer_cache_hit = True
            trace.cache_tier = card.trace.cache_tier
            return finish(card)

    # ── 4. freshness pre-check: trim a part-ingested window BEFORE compiling ─
    run = _guarded_runner(p, model)
    caveats: list[Caveat] = []
    with _Timer(trace, "freshness"):
        try:
            fresh, trimmed = dq.check_freshness(run, model, res.window)
            if fresh:
                caveats.append(fresh)
                res.window = trimmed
        except Exception as exc:  # noqa: BLE001
            log.warning("freshness check failed: %s", exc)

    # ── 5. KB probe (L1 semantic cache) ──────────────────────────────────────
    with _Timer(trace, "kb_lookup"):
        lookup = kb_retrieve.lookup(res, model.version) if use_cache else None
    if lookup:
        trace.cache_tier = lookup.tier
        trace.cache_similarity = round(lookup.top_similarity, 4)
        trace.cache_demotion_reason = lookup.demotion_reason

    # ── 6/7. plan -> compile ─────────────────────────────────────────────────
    with _Timer(trace, "plan"):
        plan, provenance = build_plan(res, model, lookup=lookup, budget=budget)
    plan_from_llm = provenance == "llm"

    try:
        with _Timer(trace, "compile"):
            cq = compile_plan(plan, model)
            bound = bind(cq.template_sql, cq.params)
    except Exception as exc:  # noqa: BLE001
        return refuse(f"The analysis plan could not be compiled: {exc}")

    # ── 8. guard (+ healing). A tier-1 guard failure is a cache MISS ─────────
    def validate(template: str) -> tuple[bool, str, str]:
        try:
            g = guard(bind(template, cq.params) if ":" in template else template, p, model)
            return True, "", g.sql
        except GuardBlocked as gb:
            return False, f"[{gb.check}] {gb.reason}", ""
        except PermissionError as pe:
            return False, str(pe), ""

    ok, why, guarded_sql = validate(cq.template_sql)
    if not ok and lookup and lookup.tier == CacheTier.EXACT_REUSE and lookup.exact:
        # Invariant 6: demote the stale record and replan from scratch.
        kb_store.mark_invalid(lookup.exact.record_id, why)
        trace.cache_demotion_reason = f"guard rejected cached SQL: {why}"
        trace.cache_tier = CacheTier.COLD
        with _Timer(trace, "replan"):
            plan, provenance = build_plan(res, model, lookup=None, budget=budget)
            plan_from_llm = provenance == "llm"
            cq = compile_plan(plan, model)
        ok, why, guarded_sql = validate(cq.template_sql)

    if not ok:
        # Unsafe/expensive/invalid and not repairable -> explicit refusal.
        try:
            with _Timer(trace, "heal"):
                outcome = run_with_healing(
                    cq.template_sql, question=question, validate=validate,
                    execute=lambda s: execute(s).rows, tables=cq.tables,
                    schema_card=model.schema_card, budget=budget)
            guarded_sql = outcome.sql
            trace.healed, trace.heal_attempts = outcome.healed, outcome.attempts
            trace.guard_first_try = outcome.guard_first_try
        except HealExhausted:
            return refuse(f"The query was rejected and could not be repaired. {why}")

    # ── 9. L2 answer cache: keyed on the POST-RBAC SQL (invariant 4) ─────────
    a_key = cache.answer_key(guarded_sql, role, watermark)
    if use_cache:
        hit = cache.get(a_key)
        if hit is not None:
            card = hit.model_copy(deep=True)
            card.question = question
            trace.answer_cache_hit = True
            cache.put(q_key, card)
            return finish(card)

    # ── 10/11. execute (+ heal) and adjudicate the row cap ──────────────────
    try:
        with _Timer(trace, "execute"):
            outcome = run_with_healing(
                cq.template_sql, question=question, validate=validate,
                execute=lambda s: execute(s).rows, tables=cq.tables,
                schema_card=model.schema_card, budget=budget)
        trace.healed, trace.heal_attempts = outcome.healed, outcome.attempts
        trace.guard_first_try = outcome.guard_first_try
        guarded_sql = outcome.sql
        exec_res = execute(guarded_sql)
    except HealExhausted as exc:
        return refuse(f"The query failed and could not be repaired: {exc.last_error}")
    except QueryTimeout as exc:
        return refuse(str(exc))

    try:
        with _Timer(trace, "row_cap"):
            exec_res = apply_row_cap(
                exec_res, bound_sql=guarded_sql, is_aggregate=cq.is_aggregate,
                count_runner=lambda s: (run(s) or [{}])[0].get("total_rows"))
    except RowCapExceeded as exc:
        return refuse(str(exc))

    rows = exec_res.rows

    # ── 12. remaining data-quality checks on the tables actually touched ────
    with _Timer(trace, "dq"):
        merchant_ids = None
        if role == "merchant_user":
            merchant_ids = [p.claim("merchant_id")]
        elif "fact_settlements" in cq.tables or plan.metric == "settled_amount":
            merchant_ids = None
        try:
            more, _ = dq.run_checks(run, model, plan.window, cq.tables,
                                    merchant_ids=merchant_ids)
            seen = {c.id for c in caveats}
            caveats += [c for c in more if c.id not in seen]
        except Exception as exc:  # noqa: BLE001
            log.warning("DQ checks failed: %s", exc)

    # ── 13. drivers (pure Python) ───────────────────────────────────────────
    with _Timer(trace, "drivers"):
        decomps = _decompose_all(rows, plan, model)

    # ── 14. chart validation ────────────────────────────────────────────────
    with _Timer(trace, "chart"):
        chart = chartmeta.validate_chart(plan.chart, rows, plan)

    # ── 15. actions, then narration over computed numbers only ──────────────
    with _Timer(trace, "actions"):
        recs = action_engine.recommend(
            decomp=decomps[0] if decomps else None, caveats=caveats, rows=rows,
            role=role, retriable_pct=None)
    with _Timer(trace, "narrate"):
        narration, _used = narrate(
            plan, decomp=decomps[0] if decomps else None, rows=rows, caveats=caveats,
            model=model, persona=rbac.persona(p, model), recommendations=recs,
            budget=budget)

    # ── 16. confidence + evidence ───────────────────────────────────────────
    conf = confidence.score(caveats=caveats, assumptions=plan.assumptions, trace=trace,
                            rows=rows)
    metric = model.metric(plan.metric)
    evidence = Evidence(
        datasets=cq.tables, sql=guarded_sql, template_sql=cq.template_sql,
        filters=[f"{f.dimension} {f.op} {f.values}" for f in plan.filters],
        window=plan.window.label(),
        baseline_window=plan.baseline.label() if plan.baseline else None,
        row_count=len(rows), truncated=exec_res.truncated,
        total_rows=exec_res.total_rows,
        metric_definition={
            "metric": metric.id, "label": metric.label,
            "numerator": metric.numerator or metric.expr or "",
            "denominator": metric.denominator or "",
            "note": " ".join((metric.denominator_note or "").split())},
        guard_checks=[], injected_predicates=[], exec_ms=exec_res.exec_ms)
    try:
        g = guard(bound, p, model, run_explain=False)
        evidence.guard_checks = g.checks_passed
        evidence.injected_predicates = g.injected_predicates
    except Exception:  # noqa: BLE001
        pass

    card = AnswerCard(
        ok=True, question=question, role=role, plan=plan, narration=narration,
        rows=rows[:500], chart=chart, decompositions=decomps, caveats=caveats,
        assumptions=plan.assumptions, confidence=conf, evidence=evidence, trace=trace)

    # ── 17. write-back ──────────────────────────────────────────────────────
    with _Timer(trace, "writeback"):
        allowed, why_not = kb_writeback.eligible(
            trace, row_count=len(rows),
            has_block_caveat=any(c.severity == "block" for c in caveats),
            truncated=exec_res.truncated, is_detail=plan.intent == "detail",
            scope_violations=0, plan_from_llm=plan_from_llm)
        try:
            if allowed:
                kb_writeback.write_back(res, plan, cq, model_version=model.version,
                                        prompt_version=config.PROMPT_VERSION,
                                        trace=trace, row_count=len(rows),
                                        exec_ms=exec_res.exec_ms)
            elif trace.healed and len(rows) > 0:
                kb_writeback.write_back(res, plan, cq, model_version=model.version,
                                        prompt_version=config.PROMPT_VERSION,
                                        trace=trace, row_count=len(rows),
                                        exec_ms=exec_res.exec_ms, quarantine=True)
            else:
                log.debug("KB write-back skipped: %s", why_not)
        except Exception as exc:  # noqa: BLE001
            log.warning("KB write-back failed: %s", exc)

    if use_cache:
        cache.put(a_key, card)
        cache.put(q_key, card)
    if lookup and lookup.exact:
        try:
            kb_store.touch(lookup.exact.record_id, success=True)
        except Exception:  # noqa: BLE001
            pass
    return finish(card)
