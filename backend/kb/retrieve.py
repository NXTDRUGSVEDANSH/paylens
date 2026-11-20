"""
Tiered cache lookup.

    tier 1  EXACT_REUSE  sim >= T1 AND shape equal AND params equal AND lexical >= floor
                         AND no residual literals  ->  reuse SQL, zero planner calls
    tier 2  FEWSHOT      sim in [T2, T1)            ->  one planner call, match as few-shot
    tier 3  COLD         sim <  T2                  ->  one planner call, schema card only

Similarity NEVER authorises reuse on its own — it only ranks candidates that already
matched the shape. That is what makes "last week" vs "last month" a safe parameter
substitution instead of a silent wrong answer.

Fractal AI's unthresholded hardcoded top-3/top-2 is deliberately not ported: on a
sparse KB it hands the model an UNRELATED query as an authoritative pattern, which is
strictly worse than no few-shot at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend import config
from backend.core.resolver import Resolution
from backend.core.schemas import CacheTier
from backend.kb import store
from backend.kb.normalize import lexical_similarity
from backend.kb.store import KBRecord

log = logging.getLogger("paylens.kb.retrieve")


@dataclass
class KBLookup:
    tier: CacheTier = CacheTier.COLD
    exact: KBRecord | None = None
    fewshots: list[KBRecord] = field(default_factory=list)
    top_similarity: float = 0.0
    demotion_reason: str | None = None


def _admit_tier1(rec: KBRecord, res: Resolution) -> tuple[bool, str | None]:
    """Every gate must pass. Each one closes a specific way the fast path goes wrong."""
    sim = rec.similarity or 0.0
    if sim < config.CACHE_T1:
        return False, f"similarity {sim:.3f} < {config.CACHE_T1}"
    if rec.meta.get("shape_key") != res.shape:
        return False, "analysis shape differs"
    if not rec.meta.get("verified", False):
        return False, "record is not verified"
    if rec.meta.get("healed", False):
        return False, "record came from a healed query"
    stored_params = sorted(rec.param_schema or {})
    if stored_params != sorted(res.param_names):
        return False, (f"parameter set differs (stored {stored_params}, "
                       f"needed {sorted(res.param_names)})")
    lex = lexical_similarity(rec.canonical_question, res.canonical_question)
    if lex < config.LEXICAL_FLOOR:
        return False, f"lexical similarity {lex:.2f} < {config.LEXICAL_FLOOR}"
    if res.residuals:
        return False, f"unresolved literals in the question: {res.residuals}"
    return True, None


def lookup(res: Resolution, model_version: str) -> KBLookup:
    if not res.canonical_question or not store.available():
        return KBLookup(demotion_reason="knowledge base unavailable")

    # Tier-1 probe: hard filters first, so similarity only ranks what already fits.
    t1_where = {"$and": [
        {"model_version": {"$eq": model_version}},
        {"shape_key": {"$eq": res.shape}},
        {"verified": {"$eq": True}},
        {"healed": {"$eq": False}},
        {"role_agnostic": {"$eq": True}},
        {"param_arity": {"$eq": len(res.param_names)}},
    ]}
    exact_candidates = store.query(res.canonical_question, where=t1_where, n=3)
    top_sim = max((c.similarity or 0.0 for c in exact_candidates), default=0.0)

    for cand in exact_candidates:
        ok, why = _admit_tier1(cand, res)
        if ok:
            return KBLookup(tier=CacheTier.EXACT_REUSE, exact=cand, top_similarity=top_sim)
        demotion = why

    # Tier-2 probe: looser, but still thresholded and still model-version scoped.
    t2_where = {"$and": [
        {"model_version": {"$eq": model_version}},
        {"role_agnostic": {"$eq": True}},
    ]}
    cands = store.query(res.canonical_question, where=t2_where, n=4)
    shots: list[KBRecord] = []
    seen_shapes: set[str] = set()
    for c in sorted(cands, key=lambda r: -((r.similarity or 0.0)
                                           * (1.0 if r.meta.get("verified") else 0.6))):
        if (c.similarity or 0.0) < config.CACHE_T2:
            continue
        shape = str(c.meta.get("shape_key"))
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        shots.append(c)
        if len(shots) >= 2:
            break

    top_sim = max(top_sim, max((c.similarity or 0.0 for c in cands), default=0.0))
    if shots:
        return KBLookup(tier=CacheTier.FEWSHOT, fewshots=shots, top_similarity=top_sim,
                        demotion_reason=demotion if exact_candidates else None)
    return KBLookup(tier=CacheTier.COLD, top_similarity=top_sim,
                    demotion_reason=demotion if exact_candidates else None)
