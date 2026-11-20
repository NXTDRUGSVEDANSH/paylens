"""
The decomposition identity must hold exactly. If it drifts, every "X% of the
decline is mix" claim in the product is unfounded.
"""
import pytest

from backend.core.drivers import contribution_ranking, decompose, rank_dimensions
from backend.core.schemas import Segment


def seg(key, weight, value, den=None):
    return Segment(key=key, weight=weight, value=value, denominator=den or weight * 100_000)


# The calibrated incident from datasets/CALIBRATION.md
BASE = [seg("UPI", 0.62, 0.8888), seg("CARD", 0.22, 0.902),
        seg("NETBANKING", 0.08, 0.712), seg("WALLET", 0.05, 0.951),
        seg("EMI", 0.03, 0.854)]
TARGET = [seg("UPI", 0.55, 0.8878), seg("CARD", 0.22, 0.901),
          seg("NETBANKING", 0.15, 0.711), seg("WALLET", 0.05, 0.950),
          seg("EMI", 0.03, 0.853)]


def test_identity_holds_exactly():
    d = decompose("payment_method", BASE, TARGET)
    assert abs(d.residual_pp) < 1e-9
    assert d.mix_pp + d.rate_pp == pytest.approx(d.delta_pp, abs=1e-9)


def test_reproduces_calibrated_figures():
    d = decompose("payment_method", BASE, TARGET)
    assert d.delta_pp == pytest.approx(-1.3376, abs=0.01)
    assert d.mix_pp == pytest.approx(-1.2376, abs=0.01)
    assert d.rate_pp == pytest.approx(-0.10, abs=0.01)
    assert d.mix_share_pct == pytest.approx(92.5, abs=0.5)
    assert d.dominant == "mix"


def test_pure_rate_change_has_zero_mix():
    """Same shares, worse rates -> all rate, no mix."""
    t = [seg("A", 0.5, 0.80), seg("B", 0.5, 0.60)]
    b = [seg("A", 0.5, 0.90), seg("B", 0.5, 0.70)]
    d = decompose("x", b, t)
    assert d.mix_pp == pytest.approx(0.0, abs=1e-9)
    assert d.rate_pp == pytest.approx(-10.0, abs=1e-9)
    assert d.dominant == "rate"


def test_pure_mix_change_has_zero_rate():
    """Identical rates, shifted shares -> all mix, no rate. The planted scenario."""
    b = [seg("GOOD", 0.9, 0.95), seg("BAD", 0.1, 0.70)]
    t = [seg("GOOD", 0.6, 0.95), seg("BAD", 0.4, 0.70)]
    d = decompose("x", b, t)
    assert d.rate_pp == pytest.approx(0.0, abs=1e-9)
    assert d.mix_pp == pytest.approx(-7.5, abs=1e-9)
    assert d.mix_share_pct == pytest.approx(100.0, abs=1e-6)


def test_simpsons_paradox_every_segment_flat_but_total_falls():
    """
    The case that breaks naive tools: no segment's rate declined at all, yet the
    overall metric fell. A threshold detector sees nothing; the decomposition
    attributes 100% to mix.
    """
    b = [seg("UPI", 0.7, 0.96), seg("NB", 0.3, 0.71)]
    t = [seg("UPI", 0.4, 0.96), seg("NB", 0.6, 0.71)]
    d = decompose("payment_method", b, t)
    assert all(s.value_target == s.value_base for s in d.segments)
    assert d.delta_pp < -5
    assert d.mix_share_pct == pytest.approx(100.0, abs=1e-6)


def test_new_segment_counts_as_mix_not_rate():
    """A rail that did not exist in the baseline cannot have 'got worse'."""
    b = [seg("A", 1.0, 0.90)]
    t = [seg("A", 0.8, 0.90), seg("NEW", 0.2, 0.50)]
    d = decompose("x", b, t)
    assert d.rate_pp == pytest.approx(0.0, abs=1e-9)
    assert abs(d.residual_pp) < 1e-9


def test_disappeared_segment_counts_as_mix():
    b = [seg("A", 0.7, 0.90), seg("GONE", 0.3, 0.40)]
    t = [seg("A", 1.0, 0.90)]
    d = decompose("x", b, t)
    assert d.rate_pp == pytest.approx(0.0, abs=1e-9)
    assert d.delta_pp > 0          # dropping a bad segment raises the total
    assert abs(d.residual_pp) < 1e-9


def test_zero_delta_does_not_divide_by_zero():
    d = decompose("x", BASE, BASE)
    assert d.delta_pp == pytest.approx(0.0, abs=1e-9)
    assert d.mix_share_pct == 0.0
    assert d.dominant == "mixed"


def test_rank_dimensions_prefers_concentrated_explanation():
    conc = decompose("payment_method", [seg("A", 0.9, 0.95), seg("B", 0.1, 0.70)],
                     [seg("A", 0.6, 0.95), seg("B", 0.4, 0.70)])
    smear = decompose("state", [seg(c, 0.25, 0.90) for c in "WXYZ"],
                      [seg(c, 0.25, 0.88) for c in "WXYZ"])
    assert rank_dimensions([smear, conc])[0].dimension == "payment_method"


def test_segments_sorted_by_absolute_impact():
    d = decompose("payment_method", BASE, TARGET)
    impacts = [abs(s.total_pp) for s in d.segments]
    assert impacts == sorted(impacts, reverse=True)
    assert d.segments[0].key in ("UPI", "NETBANKING")


def test_contribution_ranking_excludes_unmatched_segments():
    rows = contribution_ranking(BASE, TARGET)
    assert {r["segment"] for r in rows} == {s.key for s in BASE}
    assert abs(rows[0]["contribution_pp"]) >= abs(rows[-1]["contribution_pp"])
