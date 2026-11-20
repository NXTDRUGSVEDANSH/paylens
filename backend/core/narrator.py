"""
Narration. Receives ONLY computed numbers — it never queries anything and never
calculates a value, so it cannot invent one.

Output keeps `observed` / `hypotheses` / `recommendations` as separate FIELDS. The
model is structurally unable to present a guess as a fact.

If no API key is configured the deterministic templates below produce the same
structure from the same numbers. That is not a toy fallback: the numbers do the
work, so the system remains fully demonstrable offline.
"""
from __future__ import annotations

import json
import logging

from backend import config
from backend.core.schemas import (AnalysisPlan, Caveat, Decomposition, Fact,
                                  Hypothesis, Narration, Recommendation)
from backend.llm import provider
from backend.llm.provider import CallBudget
from semantic.loader import SemanticModel

log = logging.getLogger("paylens.narrator")

_SYSTEM = """You are a payments business analyst writing up a completed analysis.

ABSOLUTE RULES
- Every number you use is supplied below. Do NOT compute, estimate or invent any number.
- `observed` contains ONLY facts present in the supplied data.
- `hypotheses` contains possible EXPLANATIONS, which are NOT proven by this data.
  Never state a hypothesis as a fact. Each needs a confidence of low/medium/high.
- Do not recommend actions; a separate system does that.
- Plain text, no markdown.

Return ONLY this JSON:
{"headline": "...",
 "observed": [{"text": "...", "numbers": {}}],
 "hypotheses": [{"text": "...", "confidence": "medium", "testable_with": "..."}]}"""


def _fmt(v: float | None, kind: str = "pp") -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}pp" if kind == "pp" else f"{v:.2f}%"


def _deterministic(plan: AnalysisPlan, decomp: Decomposition | None, rows: list[dict],
                   caveats: list[Caveat], model: SemanticModel) -> Narration:
    metric = model.metric(plan.metric)
    observed: list[Fact] = []
    hypotheses: list[Hypothesis] = []

    if decomp:
        headline = (
            f"{metric.label} moved {_fmt(decomp.delta_pp)} "
            f"({decomp.baseline_value:.2f}% to {decomp.target_value:.2f}%), and "
            f"{abs(decomp.mix_share_pct):.0f}% of that is traffic mix rather than "
            f"{'degraded' if decomp.delta_pp < 0 else 'improved'} processing.")
        observed.append(Fact(
            text=(f"{metric.label} was {decomp.baseline_value:.2f}% in "
                  f"{plan.baseline.label() if plan.baseline else 'the baseline'} and "
                  f"{decomp.target_value:.2f}% in {plan.window.label()}, a change of "
                  f"{_fmt(decomp.delta_pp)}."),
            numbers={"baseline_pct": round(decomp.baseline_value, 2),
                     "target_pct": round(decomp.target_value, 2),
                     "delta_pp": round(decomp.delta_pp, 3)}))
        observed.append(Fact(
            text=(f"Decomposed by {decomp.dimension.replace('_', ' ')}: mix effect "
                  f"{_fmt(decomp.mix_pp)}, rate effect {_fmt(decomp.rate_pp)}."),
            numbers={"mix_pp": round(decomp.mix_pp, 3), "rate_pp": round(decomp.rate_pp, 3),
                     "mix_share_pct": round(decomp.mix_share_pct, 1)}))
        for s in decomp.segments[:3]:
            if abs(s.weight_target - s.weight_base) < 1e-4 and abs(s.rate_pp) < 1e-4:
                continue
            observed.append(Fact(
                text=(f"{s.key}: share {s.weight_base * 100:.2f}% to "
                      f"{s.weight_target * 100:.2f}%, own rate "
                      f"{s.value_base * 100:.2f}% to {s.value_target * 100:.2f}% "
                      f"(mix {_fmt(s.mix_pp)}, rate {_fmt(s.rate_pp)})."),
                numbers={"segment": s.key,
                         "share_base_pct": round(s.weight_base * 100, 2),
                         "share_target_pct": round(s.weight_target * 100, 2),
                         "mix_pp": round(s.mix_pp, 3), "rate_pp": round(s.rate_pp, 3)}))

        if decomp.dominant == "mix":
            gainers = [s.key for s in decomp.segments if s.weight_target > s.weight_base + 1e-4]
            hypotheses.append(Hypothesis(
                text=(f"A routing, checkout or payment-page change shifted traffic toward "
                      f"{', '.join(gainers[:2]) or 'lower-performing rails'}. "
                      f"This data shows the shift but not its cause."),
                confidence="medium",
                testable_with="checkout/routing config change log for this window"))
            hypotheses.append(Hypothesis(
                text=("Alternatively, customer behaviour shifted on its own (campaign, "
                      "seasonality, or a large merchant changing its default method)."),
                confidence="low",
                testable_with="per-merchant share shift over the same window"))
        elif decomp.dominant == "rate":
            worst = min(decomp.segments, key=lambda s: s.rate_pp)
            hypotheses.append(Hypothesis(
                text=(f"{worst.key} degraded on its own terms, which usually means an "
                      f"issuer-side or acquirer-side problem rather than a traffic change."),
                confidence="medium",
                testable_with=f"failure-code breakdown for {worst.key} in this window"))
    elif plan.intent == "detail":
        headline = (f"Showing {len(rows):,} individual attempts from "
                    f"{plan.window.label()}, most recent first.")
        observed.append(Fact(
            text=(f"This is a row-level listing, not an aggregate — the sample is "
                  f"representative but may be incomplete."),
            numbers={"rows_returned": len(rows)}))
        if rows:
            statuses: dict[str, int] = {}
            for r in rows:
                statuses[str(r.get("status"))] = statuses.get(str(r.get("status")), 0) + 1
            top = ", ".join(f"{k} {v:,}" for k, v in
                            sorted(statuses.items(), key=lambda kv: -kv[1]))
            observed.append(Fact(text=f"Status mix in the returned rows: {top}.",
                                 numbers=statuses))
    elif plan.segment_by and len(rows) > 1:
        # A breakdown must NOT report rows[0] as if it were the answer — that is
        # one segment's value wearing the headline. Summarise the distribution,
        # and compute the overall from numerator/denominator sums (never the mean
        # of ratios, which is wrong whenever segment volumes differ).
        dim = plan.segment_by[0]
        vals = [(r.get(dim), r.get(plan.metric)) for r in rows
                if r.get(plan.metric) is not None]
        vals.sort(key=lambda kv: kv[1])
        lo, hi = vals[0], vals[-1]
        num = sum(r.get("_num") or 0 for r in rows)
        den = sum(r.get("_den") or 0 for r in rows)
        overall = (num / den) if den else None
        pretty = dim.replace("_", " ")
        if metric.is_ratio:
            headline = (f"{metric.label} ranges from {lo[1] * 100:.2f}% ({lo[0]}) to "
                        f"{hi[1] * 100:.2f}% ({hi[0]}) across {len(vals)} {pretty} "
                        f"segments" + (f", {overall * 100:.2f}% overall." if overall
                                       else "."))
        else:
            headline = (f"{metric.label} ranges from {lo[1]:,.0f} ({lo[0]}) to "
                        f"{hi[1]:,.0f} ({hi[0]}) across {len(vals)} {pretty} segments.")
        observed.append(Fact(text=headline, numbers={
            "lowest": lo[0], "lowest_value": lo[1],
            "highest": hi[0], "highest_value": hi[1], "segments": len(vals),
            **({"overall": overall} if overall is not None else {})}))
        for key, v in (vals[-3:][::-1] + vals[:2]):
            if metric.is_ratio:
                observed.append(Fact(text=f"{key}: {v * 100:.2f}%",
                                     numbers={"segment": key, "value": v}))
        if overall is not None and den:
            observed.append(Fact(
                text=(f"Overall across all {pretty} segments: {overall * 100:.2f}% "
                      f"on {int(den):,} resolved attempts (computed from summed "
                      f"numerator and denominator, not by averaging the segment rates)."),
                numbers={"overall": overall, "denominator": int(den)}))
    elif rows:
        val = rows[0].get(plan.metric)
        headline = (f"{metric.label} for {plan.window.label()} is "
                    f"{val * 100:.2f}%." if metric.is_ratio and isinstance(val, (int, float))
                    else f"{metric.label} for {plan.window.label()}: {val}.")
        observed.append(Fact(text=headline, numbers={plan.metric: val}))
    else:
        headline = f"No rows matched for {plan.window.label()}."
        observed.append(Fact(text="The query ran successfully and returned no rows."))

    for c in caveats:
        if c.severity in ("warn", "block"):
            hypotheses.append(Hypothesis(
                text=f"Data quality may affect this reading: {c.message}",
                confidence="low", testable_with=f"DQ check '{c.id}'"))
    return Narration(headline=headline, observed=observed, hypotheses=hypotheses)


def narrate(plan: AnalysisPlan, *, decomp: Decomposition | None, rows: list[dict],
            caveats: list[Caveat], model: SemanticModel, persona: str,
            recommendations: list[Recommendation] | None = None,
            budget: CallBudget | None = None) -> tuple[Narration, bool]:
    """Returns (narration, used_llm). Falls back to templates without an API key."""
    base = _deterministic(plan, decomp, rows, caveats, model)
    base.recommendations = recommendations or []
    if not provider.available():
        return base, False

    payload = {
        "question_intent": plan.intent,
        "metric": {"id": plan.metric, "label": model.metric(plan.metric).label,
                   "definition_note": model.metric(plan.metric).denominator_note},
        "window": plan.window.label(),
        "baseline": plan.baseline.label() if plan.baseline else None,
        "computed_facts": [f.numbers for f in base.observed],
        "decomposition": decomp.model_dump() if decomp else None,
        "caveats": [{"severity": c.severity, "message": c.message} for c in caveats],
        "rows_sample": rows[:12],
    }
    try:
        raw = provider.call_json(
            f"{persona}\n\nAnalysis to write up:\n{json.dumps(payload, default=str)}",
            system=_SYSTEM, model=config.NARRATOR_MODEL, purpose="narrate", budget=budget)
        out = Narration(
            headline=str(raw.get("headline") or base.headline),
            observed=[Fact(text=str(f.get("text", "")), numbers=f.get("numbers") or {})
                      for f in raw.get("observed") or [] if f.get("text")] or base.observed,
            hypotheses=[Hypothesis(text=str(h.get("text", "")),
                                   confidence=h.get("confidence", "low")
                                   if h.get("confidence") in ("low", "medium", "high") else "low",
                                   testable_with=h.get("testable_with"))
                        for h in raw.get("hypotheses") or [] if h.get("text")] or base.hypotheses,
            recommendations=recommendations or [])
        return out, True
    except Exception as exc:  # noqa: BLE001
        log.warning("narration failed, using deterministic templates: %s", exc)
        return base, False
