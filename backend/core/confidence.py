"""
Confidence as arithmetic, not vibes.

"Medium confidence" from an LLM is noise — it has no calibration and no basis. These
deductions are all conditions the system can check, and the UI shows them, so
"medium" is explainable rather than decorative.

The -0.25 for self-healing is the most honest line here: if the first query was
wrong enough to need repair, the second one deserves less trust. Most systems hide
the retry; this one prices it.
"""
from __future__ import annotations

from backend.core.schemas import Assumption, Caveat, Confidence, RunTrace

BASE = 1.0
DEDUCTIONS = {
    "dq_warn": (0.15, "a data-quality warning applies to the tables used"),
    "incomplete_window": (0.20, "the time window is only partially ingested"),
    "assumed_input": (0.15, "an unstated time window or denominator was assumed"),
    "small_sample": (0.10, "a reported segment has fewer than 500 attempts"),
    "healed": (0.25, "the query had to be repaired before it would run"),
    "cache_demoted": (0.05, "a cached query was rejected and regenerated"),
}
GOLDEN_BONUS = (0.10, "matches a known-answer evaluation question")
SMALL_SAMPLE_THRESHOLD = 500


def score(*, caveats: list[Caveat], assumptions: list[Assumption], trace: RunTrace,
          rows: list[dict], min_sample_col: str = "attempts") -> Confidence:
    deductions: list[dict] = []
    total = BASE

    def apply(key: str, extra: dict | None = None) -> None:
        nonlocal total
        amount, why = DEDUCTIONS[key]
        total -= amount
        deductions.append({"reason": key, "amount": -amount, "explanation": why,
                           **(extra or {})})

    ids = {c.id for c in caveats}
    if any(c.severity == "warn" for c in caveats):
        apply("dq_warn", {"caveats": sorted(ids)})
    if "incomplete_window" in ids:
        apply("incomplete_window")
    if assumptions:
        apply("assumed_input", {"assumptions": [a.kind for a in assumptions]})

    samples = [r[min_sample_col] for r in rows
               if isinstance(r.get(min_sample_col), (int, float))]
    if samples and min(samples) < SMALL_SAMPLE_THRESHOLD:
        apply("small_sample", {"smallest_segment": int(min(samples))})

    if trace.healed:
        apply("healed", {"attempts": trace.heal_attempts})
    if trace.cache_demotion_reason:
        apply("cache_demoted", {"why": trace.cache_demotion_reason})

    if trace.golden_id:
        amount, why = GOLDEN_BONUS
        total += amount
        deductions.append({"reason": "golden_question", "amount": +amount,
                           "explanation": why, "golden_id": trace.golden_id})

    total = max(0.0, min(1.0, total))
    bucket = "high" if total >= 0.75 else "medium" if total >= 0.50 else "low"
    return Confidence(score=round(total, 3), bucket=bucket, deductions=deductions)
