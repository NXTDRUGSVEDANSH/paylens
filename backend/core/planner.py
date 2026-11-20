"""
The planner. Produces a validated AnalysisPlan — never SQL directly.

Three paths, cheapest first:

  plan_from_record       tier-1 cache hit: rehydrate the stored plan, ZERO LLM calls
  plan_from_resolution   deterministic resolution was confident: ZERO LLM calls
  plan_from_llm          ambiguous: ONE call, with any tier-2 match as a few-shot

Most questions never reach the model. That is the point of putting a deterministic
resolver in front of it: the LLM is for genuine ambiguity, not for routine parsing.
"""
from __future__ import annotations

import json
import logging

from backend import config
from backend.core.resolver import Resolution
from backend.core.schemas import (AnalysisPlan, AnswerStrategy, Assumption, ChartSpec,
                                  Filter, TimeWindow)
from backend.kb.store import KBRecord
from backend.llm import provider
from backend.llm.provider import CallBudget
from semantic.loader import SemanticModel

log = logging.getLogger("paylens.planner")

_STRATEGY_FOR_INTENT = {
    "explain_change": ("driver_decomposition",
                       ["mix_effect", "rate_effect", "top_contributor", "share_explained"]),
    "trend": ("time_series_trend", ["peak", "trough", "direction", "change_pct"]),
    "rank": ("top_n_ranked", ["winner", "runner_up", "spread", "total"]),
    "breakdown": ("distribution", ["dominant_segment", "total", "segment_count"]),
    "compare": ("comparison", ["group_a_value", "group_b_value", "difference"]),
    "single_value": ("single_aggregate", ["exact_value"]),
    "detail": ("detail_sample", ["total_count", "common_pattern"]),
}

_SYSTEM = """You are a payments analytics planner. You translate a business question
into a STRUCTURED ANALYSIS PLAN. You never write SQL.

You may ONLY use metric ids and dimension ids from the supplied semantic model.
If the question cannot be expressed with them, pick the closest metric and say so in
`assumptions`.

Return ONLY this JSON:
{"intent": "single_value|trend|breakdown|compare|explain_change|rank|detail",
 "metric": "<metric id>",
 "secondary_metrics": [],
 "segment_by": ["<dimension id>"],
 "time_grain": "hour|day|week|month|null",
 "top_n": null,
 "needs_decomposition": false,
 "chart": {"chart_type": "line|bar|horizontal_bar|stacked_bar|waterfall|pie|kpi|table",
           "x": "<result column>", "y": "<result column>", "series": null, "title": "..."},
 "answer_strategy": {"type": "...", "focus": ["..."]},
 "assumptions": [{"kind": "metric|segment|grain|other", "statement": "...",
                  "alternative": null}]}"""


def _default_chart(intent: str, metric: str, segment_by: list[str],
                   grain: str | None) -> ChartSpec:
    if intent == "explain_change":
        return ChartSpec(chart_type="waterfall", x="component", y=metric,
                         title="What moved the metric")
    if grain:
        return ChartSpec(chart_type="line", x="bucket", y=metric,
                         series=segment_by[0] if segment_by else None,
                         title=f"{metric.replace('_', ' ').title()} over time")
    if segment_by:
        return ChartSpec(chart_type="bar", x=segment_by[0], y=metric,
                         title=f"{metric.replace('_', ' ').title()} by "
                               f"{segment_by[0].replace('_', ' ')}")
    return ChartSpec(chart_type="kpi", y=metric, title=metric.replace("_", " ").title())


def plan_from_resolution(res: Resolution, model: SemanticModel) -> AnalysisPlan:
    """Deterministic plan. No LLM."""
    metric = res.metrics[0] if res.metrics else "success_rate"
    segment_by = list(res.dimensions)
    intent = res.intent

    # explain_change is only meaningful against a baseline and a decomposable metric,
    # and it needs something to decompose BY.
    needs_decomp = False
    if intent == "explain_change" and res.baseline:
        m = model.metric(metric)
        if m.decomposable:
            needs_decomp = True
            if not segment_by:
                segment_by = ["payment_method"]
    strategy, focus = _STRATEGY_FOR_INTENT.get(intent, ("single_aggregate", ["exact_value"]))

    return AnalysisPlan(
        intent=intent, metric=metric, window=res.window,
        baseline=res.baseline if intent in ("explain_change", "compare") else None,
        segment_by=segment_by, filters=list(res.filters), time_grain=res.time_grain,
        top_n=res.top_n, needs_decomposition=needs_decomp,
        chart=_default_chart(intent, metric, segment_by, res.time_grain),
        answer_strategy=AnswerStrategy(type=strategy, focus=focus),
        assumptions=list(res.assumptions))


def plan_from_record(rec: KBRecord, res: Resolution, model: SemanticModel) -> AnalysisPlan:
    """
    Tier-1 rehydrate: take the stored plan's SHAPE but rebind this request's window,
    filters and assumptions. The stored dates are deliberately not reused.
    """
    try:
        stored = AnalysisPlan.model_validate(rec.plan)
    except Exception as exc:  # noqa: BLE001
        log.warning("stored plan failed validation, falling back: %s", exc)
        return plan_from_resolution(res, model)
    return stored.model_copy(update={
        "window": res.window,
        "baseline": res.baseline if stored.baseline else None,
        "filters": list(res.filters),
        "top_n": res.top_n if res.top_n is not None else stored.top_n,
        "assumptions": list(res.assumptions),
    })


def plan_from_llm(res: Resolution, model: SemanticModel, *,
                  fewshots: list[KBRecord] | None = None,
                  budget: CallBudget | None = None) -> tuple[AnalysisPlan, bool]:
    """One LLM call. Returns (plan, used_llm); falls back deterministically."""
    fallback = plan_from_resolution(res, model)
    if not provider.available():
        return fallback, False

    shots = ""
    for s in (fewshots or [])[:2]:
        shots += (f"\nA similar question that was answered correctly before:\n"
                  f"  question: {s.raw_question}\n"
                  f"  plan: {json.dumps(s.plan, default=str)[:900]}\n")

    prompt = f"""{model.schema_card}

{shots}
Question: {res.question}

Resolved deterministically (use unless clearly wrong):
  candidate metrics:    {res.metrics or 'none matched'}
  candidate dimensions: {res.dimensions or 'none matched'}
  time window:          {res.window.label() if res.window else 'unresolved'}
  baseline window:      {res.baseline.label() if res.baseline else 'none'}
  suggested intent:     {res.intent}
  ambiguities:          {res.ambiguities or 'none'}

Produce the analysis plan."""
    try:
        raw = provider.call_json(prompt, system=_SYSTEM, model=config.PLANNER_MODEL,
                                 purpose="plan", budget=budget)
    except Exception as exc:  # noqa: BLE001
        log.warning("planner call failed, using deterministic plan: %s", exc)
        return fallback, False

    try:
        metric = str(raw.get("metric") or fallback.metric)
        if metric not in model.metrics:
            raise ValueError(f"unknown metric '{metric}'")
        segs = [d for d in (raw.get("segment_by") or []) if d in model.dimensions]
        intent = raw.get("intent") if raw.get("intent") in model.intents else fallback.intent
        grain = raw.get("time_grain")
        grain = grain if grain in model.time_grains else None
        secondary = [s for s in (raw.get("secondary_metrics") or []) if s in model.metrics]

        chart = None
        if isinstance(raw.get("chart"), dict):
            try:
                chart = ChartSpec.model_validate(raw["chart"])
            except Exception:  # noqa: BLE001
                chart = None
        strat = None
        if isinstance(raw.get("answer_strategy"), dict):
            try:
                strat = AnswerStrategy.model_validate(raw["answer_strategy"])
            except Exception:  # noqa: BLE001
                strat = None

        extra = []
        for a in raw.get("assumptions") or []:
            if isinstance(a, dict) and a.get("statement"):
                kind = a.get("kind")
                extra.append(Assumption(
                    kind=kind if kind in ("time_window", "denominator", "grain", "metric",
                                          "segment", "other") else "other",
                    statement=str(a["statement"]), alternative=a.get("alternative")))

        needs = bool(raw.get("needs_decomposition")) and bool(res.baseline) \
            and model.metric(metric).decomposable
        if intent == "explain_change" and res.baseline and model.metric(metric).decomposable:
            needs = True
            if not segs:
                segs = ["payment_method"]

        plan = AnalysisPlan(
            intent=intent, metric=metric, secondary_metrics=secondary,
            window=res.window, baseline=res.baseline if intent in ("explain_change", "compare") else None,
            segment_by=segs, filters=list(res.filters), time_grain=grain,
            top_n=raw.get("top_n") if isinstance(raw.get("top_n"), int) else res.top_n,
            needs_decomposition=needs,
            chart=chart or _default_chart(intent, metric, segs, grain),
            answer_strategy=strat or AnswerStrategy(
                type=_STRATEGY_FOR_INTENT.get(intent, ("single_aggregate", []))[0],
                focus=_STRATEGY_FOR_INTENT.get(intent, ("", ["exact_value"]))[1]),
            assumptions=list(res.assumptions) + extra)
        return plan, True
    except Exception as exc:  # noqa: BLE001
        log.warning("planner output rejected (%s), using deterministic plan", exc)
        return fallback, False


def build_plan(res: Resolution, model: SemanticModel, *, lookup=None,
               budget: CallBudget | None = None) -> tuple[AnalysisPlan, str]:
    """Returns (plan, provenance) where provenance is cache|deterministic|llm."""
    from backend.core.schemas import CacheTier
    if lookup is not None and lookup.tier == CacheTier.EXACT_REUSE and lookup.exact:
        return plan_from_record(lookup.exact, res, model), "cache"
    if res.is_confident:
        return plan_from_resolution(res, model), "deterministic"
    plan, used = plan_from_llm(
        res, model, fewshots=(lookup.fewshots if lookup else None), budget=budget)
    return plan, "llm" if used else "deterministic"
