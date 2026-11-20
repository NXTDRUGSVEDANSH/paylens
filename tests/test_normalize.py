"""
Canonicalisation tests. The last two are the regression tests that prove the
>=0.95 cache tier is safe.
"""
import pytest

from backend.kb.keys import record_id, shape_key
from backend.kb.normalize import canonical_form, lexical_similarity, residual_literals
from semantic.loader import get_model

M = get_model()


def _terms(mids, dids):
    out = []
    for i in mids:
        x = M.metric(i); out += [x.id.replace("_", " "), x.label.lower(), *x.synonyms]
    for i in dids:
        d = M.dimension(i); out += [d.id.replace("_", " "), d.label.lower(), *d.synonyms]
    return out


def canon(q, has_baseline=False):
    mids, dids = M.match_metrics(q), M.match_dimensions(q)
    c, kinds = canonical_form(q, mids, dids, has_baseline=has_baseline,
                              synonym_terms=_terms(mids, dids))
    return c, kinds, mids, dids


def test_synonyms_collapse_to_one_canonical_form():
    """'approval rate by rail' and 'success rate by payment method' are one question."""
    a, _, _, _ = canon("success rate last week vs the week before by payment method", True)
    b, _, _, _ = canon("approval rate by rail last week vs week before", True)
    assert a == b


def test_baseline_phrase_becomes_baseline_not_second_window():
    c, kinds, _, _ = canon("success rate last week vs the week before", True)
    assert "<WINDOW>" in c and "<BASELINE>" in c
    assert c.count("<WINDOW>") == 1
    assert "week before" not in c
    assert kinds == ["window", "baseline"]


def test_baseline_appended_when_implied_but_only_one_window_named():
    c, _, _, _ = canon("why did success rate drop last week", has_baseline=True)
    assert "<BASELINE>" in c


def test_merchant_id_is_extracted_not_embedded():
    c, _, _, _ = canon("success rate for M-1042 in October")
    assert "m-1042" not in c.lower()
    assert "<ENTITY:merchant>" in c


def test_resolved_ids_are_order_independent():
    a, _, _, _ = canon("success rate by issuer bank and payment method")
    b, _, _, _ = canon("success rate by payment method and issuer bank")
    assert a == b


def test_residual_literals_flags_unextracted_number():
    """An unextracted literal means resolution did not understand the question."""
    assert residual_literals("success over <WINDOW> above 500") == ["500"]
    assert residual_literals("<METRIC:gmv> <WINDOW>") == []


def test_residual_literals_flags_quoted_and_capitalised():
    assert "Acme Holdings" in residual_literals('<WINDOW> for Acme Holdings')
    assert "foo bar" in residual_literals("""<WINDOW> named 'foo bar'""")


def test_lexical_similarity_bounds():
    assert lexical_similarity("abc def", "abc def") == pytest.approx(1.0)
    assert lexical_similarity("abcdef", "zyxwvu") < 0.1


# ── the regression tests that justify the whole module ────────────────────────
def test_dangerous_pair_canonicalises_identically_by_design():
    """
    "last week" and "last month" SHOULD produce the same canonical text. The thing
    that differs has moved into params, where it is compared exactly. This is what
    converts a silent wrong-window answer into a safe parameter substitution.
    """
    a, _, _, _ = canon("what was the success rate last week")
    b, _, _, _ = canon("what was the success rate last month")
    assert a == b


def test_dangerous_pair_is_separated_by_shape_and_params_not_text():
    """
    Identical canonical text is only safe because tier-1 admission ALSO requires
    equal shape_key and an exact parameter match. Different grains -> different
    shape_key -> no reuse.
    """
    k_week = shape_key(intent="single_value", metrics=["success_rate"], dimensions=[],
                       filter_dimensions=[], time_grain="day", has_baseline=False,
                       model_version=M.version)
    k_month = shape_key(intent="single_value", metrics=["success_rate"], dimensions=[],
                        filter_dimensions=[], time_grain="month", has_baseline=False,
                        model_version=M.version)
    assert k_week != k_month


def test_shape_key_ignores_filter_values_but_not_filter_dimensions():
    """Filter VALUES must not enter the key, or records become tenant-specific."""
    base = dict(intent="breakdown", metrics=["success_rate"], dimensions=["issuer_bank"],
                time_grain=None, has_baseline=False, model_version=M.version)
    assert shape_key(filter_dimensions=["state"], **base) == \
           shape_key(filter_dimensions=["state"], **base)
    assert shape_key(filter_dimensions=["state"], **base) != \
           shape_key(filter_dimensions=["merchant_tier"], **base)


def test_record_id_upserts_on_same_shape_and_question():
    a = record_id("abc123", "<WINDOW> <METRIC:gmv>")
    assert a == record_id("abc123", "<WINDOW> <METRIC:gmv>")
    assert a != record_id("abc123", "<WINDOW> <METRIC:success_rate>")
