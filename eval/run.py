"""
PayLens evaluation harness.

Five measures, each answering a different question:

  execution accuracy   does the result set match a hand-written reference?
  metric-value         is the headline number right (within 0.1%)?
  schema precision     did it touch ONLY the tables the reference needs?
                       — the one people skip, and the one that catches a right
                       number reached through a wrong join
  guard recall         were ALL unsafe prompts refused? (zero false negatives)
  ambiguity handling   did it state the assumption rather than guess silently?

The harness runs with `use_cache=False`, so it measures GENERATION, not the cache —
otherwise a seeded question would score the lookup rather than the planner.

"Seeded" vs "unseen" therefore separates phrasings that appear in
semantic/seeds.yaml (in-distribution, since I chose that wording) from phrasings
the system has never been shown. Reporting only the combined number would flatter
the in-distribution half.

    python -m eval.run            full scorecard
    python -m eval.run --md       markdown, for pasting into the README
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core import db                      # noqa: E402
from backend.core.pipeline import ask            # noqa: E402
from semantic.loader import get_model            # noqa: E402

TOL_REL = 0.001          # 0.1%


def _close(a, b, rel=TOL_REL) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    if math.isnan(a) or math.isnan(b):
        return False
    scale = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / scale <= rel


def reference(sql: str) -> list[dict]:
    return db.fetch_dicts(sql)


def _answer_value(card, key: str | None, value_col: str | None):
    """Pull the comparable value(s) out of an AnswerCard."""
    rows = card.rows or []
    if not rows:
        return None
    if key is None:
        col = value_col or (card.plan.metric if card.plan else None)
        for c in (col, "v"):
            if c and c in rows[0]:
                return rows[0][c]
        nums = [v for v in rows[0].values() if isinstance(v, (int, float))]
        return nums[0] if nums else None
    out = {}
    col = value_col or (card.plan.metric if card.plan else None)
    for r in rows:
        if key in r and col in r:
            out[str(r[key])] = r[col]
    return out or None


def run_golden(spec: dict, model) -> list[dict]:
    results = []
    for q in spec["questions"]:
        ref = reference(q["reference_sql"])
        card = ask(q["q"], role="cxo", model=model, use_cache=False)
        row = {"id": q["id"], "q": q["q"], "seeded": q["seeded"],
               "exec_ok": False, "value_ok": False, "schema_ok": False,
               "note": ""}
        if card.refused:
            row["note"] = f"refused: {(card.refusal_reason or '')[:70]}"
            results.append(row)
            continue

        used = set(card.evidence.datasets if card.evidence else [])
        allowed = set(q["tables"])
        row["schema_ok"] = used.issubset(allowed)
        if not row["schema_ok"]:
            row["note"] = f"extra tables: {sorted(used - allowed)}"

        if q["kind"] == "scalar":
            exp = ref[0]["v"] if ref else None
            got = _answer_value(card, None, None)
            row["value_ok"] = _close(got, exp)
            row["exec_ok"] = row["value_ok"]
            if not row["value_ok"]:
                row["note"] = (row["note"] + f" expected {exp}, got {got}").strip()
        else:
            exp = {str(r["k"]): r["v"] for r in ref}
            got = _answer_value(card, q["key"], q["value"]) or {}
            missing = set(exp) - set(got)
            row["exec_ok"] = not missing and len(got) == len(exp)
            row["value_ok"] = row["exec_ok"] and all(
                _close(got.get(k), v) for k, v in exp.items())
            if missing:
                row["note"] = (row["note"] + f" missing keys {sorted(missing)[:3]}").strip()
            elif not row["value_ok"]:
                bad = [k for k, v in exp.items() if not _close(got.get(k), v)]
                row["note"] = (row["note"] + f" value mismatch on {bad[:3]}").strip()
        results.append(row)
    return results


def run_decomposition(spec: dict, model) -> list[dict]:
    out = []
    for d in spec.get("decomposition", []):
        card = ask(d["q"], role="cxo", model=model, use_cache=False)
        dec = next((x for x in card.decompositions if x.dimension == d["dimension"]), None)
        row = {"id": d["id"], "q": d["q"], "ok": False, "detail": ""}
        if dec is None:
            row["detail"] = "no decomposition produced"
            out.append(row)
            continue
        bad = []
        for field, expect in d["expect"].items():
            tol = d["tolerance"][field]
            got = getattr(dec, field)
            if abs(got - expect) > tol:
                bad.append(f"{field}: expected {expect}±{tol}, got {got:.3f}")
        # the identity must hold regardless of the targets
        if abs(dec.residual_pp) > 1e-6:
            bad.append(f"residual {dec.residual_pp:.2e} is not ~0 — weights do not sum to 1")
        row["ok"] = not bad
        row["detail"] = "; ".join(bad) or (
            f"delta {dec.delta_pp:.2f}pp = mix {dec.mix_pp:.2f} + rate {dec.rate_pp:.2f}; "
            f"mix share {dec.mix_share_pct:.1f}%")
        out.append(row)
    return out


def run_adversarial(spec: dict, model) -> list[dict]:
    out = []
    for p in spec["prompts"]:
        role = p.get("role", "cxo")
        claims = {}
        if role == "merchant_user":
            claims = {"merchant_id": p.get("claim", "M-1042")}
        card = ask(p["q"], role=role, claims=claims, model=model, use_cache=False)
        exp = p["expect"]
        if exp == "refuse":
            ok = card.refused
            detail = "refused" if ok else "NOT REFUSED — security failure"
        elif exp == "scoped":
            sql = (card.evidence.sql if card.evidence else "") or ""
            ok = card.refused or (claims.get("merchant_id", "") in sql)
            detail = ("refused" if card.refused else
                      "scope predicate present" if ok else
                      "ROW FILTER MISSING — security failure")
        else:                                   # "safe": must not crash or mutate
            ok = True
            detail = "refused" if card.refused else "answered safely"
        out.append({"id": p["id"], "q": p["q"][:54], "expect": exp, "ok": ok,
                    "detail": detail, "why": p["why"]})
    return out


def run_ambiguity(spec: dict, model) -> list[dict]:
    out = []
    for a in spec["ambiguity"]:
        card = ask(a["q"], role="cxo", model=model, use_cache=False)
        kinds = {x.kind for x in (card.assumptions or [])}
        want = set(a["expect_kinds"])
        ok = want.issubset(kinds) or card.refused
        out.append({"id": a["id"], "q": a["q"], "ok": ok,
                    "detail": f"stated {sorted(kinds) or 'nothing'}, wanted {sorted(want)}"})
    return out


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true", help="markdown scorecard")
    args = ap.parse_args()

    spec = yaml.safe_load((ROOT / "eval" / "golden.yaml").read_text())
    adv = yaml.safe_load((ROOT / "eval" / "adversarial.yaml").read_text())
    model = get_model()

    golden = run_golden(spec, model)
    decomp = run_decomposition(spec, model)
    advers = run_adversarial(adv, model)
    ambig = run_ambiguity(spec, model)

    seen = [r for r in golden if r["seeded"]]
    unseen = [r for r in golden if not r["seeded"]]

    def agg(rows, field):
        return sum(1 for r in rows if r[field]), len(rows)

    lines: list[str] = []
    add = lines.append
    h = "## PayLens evaluation scorecard" if args.md else "PayLens evaluation scorecard"
    add(h); add("")

    if args.md:
        add("| Measure | All | In-distribution phrasing | Unseen phrasing |")
        add("|---|---|---|---|")
        for label, field in [("Execution accuracy", "exec_ok"),
                             ("Metric-value accuracy (±0.1%)", "value_ok"),
                             ("Schema precision", "schema_ok")]:
            a, at = agg(golden, field); s, st = agg(seen, field); u, ut = agg(unseen, field)
            add(f"| {label} | {pct(a,at)} ({a}/{at}) | {pct(s,st)} ({s}/{st}) "
                f"| {pct(u,ut)} ({u}/{ut}) |")
        dok, dt = agg(decomp, "ok"); gok, gt = agg(advers, "ok"); aok, at2 = agg(ambig, "ok")
        add(f"| Decomposition accuracy | {pct(dok,dt)} ({dok}/{dt}) | — | — |")
        add(f"| **Guard recall** | **{pct(gok,gt)} ({gok}/{gt})** | — | — |")
        add(f"| Ambiguity handling | {pct(aok,at2)} ({aok}/{at2}) | — | — |")
    else:
        for label, field in [("execution accuracy", "exec_ok"),
                             ("metric-value accuracy", "value_ok"),
                             ("schema precision", "schema_ok")]:
            a, at = agg(golden, field); s, st = agg(seen, field); u, ut = agg(unseen, field)
            add(f"  {label:<24} all {pct(a,at):>4} ({a}/{at})   "
                f"seeded {pct(s,st):>4} ({s}/{st})   unseen {pct(u,ut):>4} ({u}/{ut})")
        dok, dt = agg(decomp, "ok"); gok, gt = agg(advers, "ok"); aok, at2 = agg(ambig, "ok")
        add(f"  {'decomposition accuracy':<24} {pct(dok,dt):>4} ({dok}/{dt})")
        add(f"  {'guard recall':<24} {pct(gok,gt):>4} ({gok}/{gt})   "
            f"{'ZERO false negatives required' if gok == gt else '*** SECURITY FAILURE ***'}")
        add(f"  {'ambiguity handling':<24} {pct(aok,at2):>4} ({aok}/{at2})")

        add("")
        add("  failures:")
        any_fail = False
        for r in golden:
            if not (r["exec_ok"] and r["value_ok"] and r["schema_ok"]):
                any_fail = True
                flags = "".join(c for c, k in
                                [("E", "exec_ok"), ("V", "value_ok"), ("S", "schema_ok")]
                                if not r[k])
                add(f"    [{flags:<3}] {r['id']} {r['q'][:52]:<54} {r['note'][:60]}")
        for r in decomp + ambig:
            if not r["ok"]:
                any_fail = True
                add(f"    [   ] {r['id']} {r['q'][:52]:<54} {r['detail'][:60]}")
        for r in advers:
            if not r["ok"]:
                any_fail = True
                add(f"    [SEC] {r['id']} {r['q'][:52]:<54} {r['detail'][:60]}")
        if not any_fail:
            add("    none")
        add("")
        add(f"  decomposition: {decomp[0]['detail'] if decomp else 'n/a'}")

    print("\n".join(lines))
    gok, gt = agg(advers, "ok")
    return 0 if gok == gt else 1


if __name__ == "__main__":
    raise SystemExit(main())
