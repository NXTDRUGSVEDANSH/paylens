"""
Driver engine. Pure Python, no LLM — this is where the assignment is won.

A ratio metric can decline for two completely different reasons, and conflating
them is the classic analyst error:

    MIX  effect — traffic moved toward structurally worse segments
    RATE effect — the segments themselves got worse

    delta = sum (w_t - w_b) * v_b        [mix]
          + sum  w_t * (v_t - v_b)       [rate]

The identity is exact, which is why `residual_pp` is asserted at ~0: if it drifts,
the segment weights do not sum to 1 and the result is not trustworthy (that is also
why a truncated GROUP BY must be blocked rather than truncated).
"""
from __future__ import annotations

from backend.core.schemas import Decomposition, Segment, SegmentEffect


def _normalise(segs: list[Segment]) -> dict[str, Segment]:
    total = sum(s.denominator if s.denominator is not None else s.weight for s in segs)
    out: dict[str, Segment] = {}
    for s in segs:
        den = s.denominator if s.denominator is not None else s.weight
        out[s.key] = Segment(key=s.key, weight=(den / total) if total else 0.0,
                             value=s.value, numerator=s.numerator, denominator=s.denominator)
    return out


def decompose(dimension: str, baseline: list[Segment], target: list[Segment],
              *, as_pp: bool = True) -> Decomposition:
    """
    Split the change in a ratio metric into mix and rate effects.

    A segment present in only one period contributes purely MIX (its rate is
    carried over from the period where it exists), which is the honest treatment:
    a rail that did not exist last week cannot have "got worse".
    """
    b, t = _normalise(baseline), _normalise(target)
    keys = sorted(set(b) | set(t))
    scale = 100.0 if as_pp else 1.0

    base_value = sum(s.weight * s.value for s in b.values())
    target_value = sum(s.weight * s.value for s in t.values())

    effects: list[SegmentEffect] = []
    mix_total = rate_total = 0.0
    for k in keys:
        bs, ts = b.get(k), t.get(k)
        w_b = bs.weight if bs else 0.0
        w_t = ts.weight if ts else 0.0
        v_b = bs.value if bs else (ts.value if ts else 0.0)
        v_t = ts.value if ts else (bs.value if bs else 0.0)
        mix = (w_t - w_b) * v_b
        rate = w_t * (v_t - v_b)
        mix_total += mix
        rate_total += rate
        effects.append(SegmentEffect(
            key=k, mix_pp=mix * scale, rate_pp=rate * scale,
            total_pp=(mix + rate) * scale, weight_base=w_b, weight_target=w_t,
            value_base=v_b, value_target=v_t))

    delta = target_value - base_value
    effects.sort(key=lambda e: -abs(e.total_pp))
    return Decomposition(
        dimension=dimension,
        baseline_value=base_value * scale, target_value=target_value * scale,
        delta_pp=delta * scale, mix_pp=mix_total * scale, rate_pp=rate_total * scale,
        residual_pp=(delta - mix_total - rate_total) * scale,
        mix_share_pct=(100.0 * mix_total / delta) if abs(delta) > 1e-12 else 0.0,
        segments=effects)


def rank_dimensions(decomps: list[Decomposition]) -> list[Decomposition]:
    """
    Order dimensions by how much of the change each actually explains.

    Ranking by |delta| is useless (every dimension reports the same overall delta).
    What differs is concentration: a dimension where one or two segments account for
    nearly all the movement is a better explanation than one where it is smeared
    evenly, because it points somewhere specific.
    """
    def concentration(d: Decomposition) -> float:
        tot = sum(abs(s.total_pp) for s in d.segments) or 1e-12
        top2 = sum(abs(s.total_pp) for s in d.segments[:2])
        return top2 / tot

    return sorted(decomps, key=lambda d: -concentration(d))


def contribution_ranking(baseline: list[Segment], target: list[Segment]) -> list[dict]:
    """Per-segment contribution to the overall change, volume-weighted."""
    b, t = _normalise(baseline), _normalise(target)
    out = []
    for k in sorted(set(b) | set(t)):
        bs, ts = b.get(k), t.get(k)
        if not bs or not ts:
            continue
        out.append({
            "segment": k,
            "delta_pp": (ts.value - bs.value) * 100,
            "weight_pct": ts.weight * 100,
            "contribution_pp": ts.weight * (ts.value - bs.value) * 100,
            "volume": ts.denominator,
        })
    return sorted(out, key=lambda r: -abs(r["contribution_pp"]))
