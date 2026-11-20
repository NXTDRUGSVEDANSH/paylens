"""
Action Engine — the `recommendation/` engine from Fractal AI, re-aimed at payments.

The brief grades this: "distinguish observed facts from hypotheses AND
recommendations". A recommendation generated inside the narration call is weak,
because the model is simultaneously summarising numbers and inventing advice, and
advice is where hallucination costs most. So this runs separately, and its input is
the FINISHED analysis, not the raw data.

Preconditions are evaluated in Python. The catalogue is in semantic/actions.yaml.
"""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

from backend import config
from backend.core.schemas import Caveat, Decomposition, Recommendation


@functools.lru_cache(maxsize=1)
def catalogue() -> list[dict]:
    path = Path(config.SEMANTIC_MODEL_PATH).parent / "actions.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["actions"]


def _facts(decomp: Decomposition | None, rows: list[dict], caveats: list[Caveat]) -> dict:
    f: dict = {"delta": 0.0, "mix_share": 0.0, "max_drift": 0.0,
               "top_segment": "", "top_contribution": 0.0,
               "retriable_pct": 0.0, "affected_n": 0}
    if decomp:
        f["delta"] = decomp.delta_pp
        f["mix_share"] = abs(decomp.mix_share_pct)
        if decomp.segments:
            f["max_drift"] = max(abs(s.value_target - s.value_base) * 100
                                 for s in decomp.segments)
            worst = min(decomp.segments, key=lambda s: s.rate_pp)
            f["top_segment"] = worst.key
            f["top_contribution"] = worst.rate_pp
    for c in caveats:
        if c.id == "settlement_mismatch":
            f["affected_n"] = len(c.evidence.get("affected_merchants", []))
    return f


def _fires(when: str, decomp: Decomposition | None, f: dict, caveats: list[Caveat],
           rows: list[dict]) -> bool:
    ids = {c.id for c in caveats}
    if when == "settlement_disagreement":
        return "settlement_mismatch" in ids
    if when == "no_material_change":
        return decomp is not None and abs(decomp.delta_pp) < 0.25
    if decomp is None:
        return False
    if when == "mix_dominant":
        return decomp.dominant == "mix" and abs(decomp.delta_pp) >= 0.25
    if when == "rate_dominant_single_issuer":
        return (decomp.dominant == "rate" and abs(decomp.delta_pp) >= 0.25
                and bool(f["top_segment"]))
    if when == "high_retriable_failure_share":
        return f["retriable_pct"] >= 60.0
    if when in ("timeout_rising_with_latency", "acquirer_underperforms_on_same_mix",
                "risk_block_share_rising_low_risk_tier"):
        return False        # need signals this query did not fetch; see ASSUMPTIONS.md
    return False


def recommend(*, decomp: Decomposition | None, caveats: list[Caveat], rows: list[dict],
              role: str, retriable_pct: float | None = None) -> list[Recommendation]:
    f = _facts(decomp, rows, caveats)
    if retriable_pct is not None:
        f["retriable_pct"] = retriable_pct

    out: list[Recommendation] = []
    for a in catalogue():
        if role not in a.get("roles", []):
            continue
        if not _fires(a["when"], decomp, f, caveats, rows):
            continue
        try:
            action = " ".join(str(a["action"]).split()).format(**f)
            why = " ".join(str(a["rationale_template"]).split()).format(**f)
        except (KeyError, ValueError):
            continue
        out.append(Recommendation(action=action, owner=a["owner"], rationale=why,
                                  precondition=a["when"]))
    return out
