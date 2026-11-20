"""
Verify the generated data reproduces what the calibration promises.

This is the acceptance test for Phase 1. If the mix share drifts away from the
expected value, the demo's headline claim is false and the driver engine will be
built against a signal that is not really there.

Run:  python data/verify.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from npci_calibration import (  # noqa: E402
    UPI_BUSINESS_DECLINE_PCT, UPI_TECHNICAL_DECLINE_PCT, expected_decomposition,
)
from generate import BASELINE_WEEK, TARGET_WEEK  # noqa: E402

DB = Path(__file__).resolve().parent / "paylens.duckdb"
DEDUP = """
  select * from (
    select *, row_number() over (partition by txn_id order by ingested_at desc) rn
    from fact_transactions) where rn = 1
"""
FAILS: list[str] = []


def check(label: str, actual: float, expected: float, tol: float, unit: str = "pp") -> None:
    ok = abs(actual - expected) <= tol
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label:<42} actual {actual:>9.4f}{unit}  expected {expected:>9.4f}{unit}  tol ±{tol}")
    if not ok:
        FAILS.append(label)


def segments(con, lo, hi) -> dict[str, tuple[float, float]]:
    """{method: (volume_share, success_rate)} over [lo, hi]."""
    rows = con.execute(f"""
        with t as ({DEDUP})
        select pm.method,
               count(*) filter (where t.status in ('SUCCESS','FAILED'))              as denom,
               count(*) filter (where t.status = 'SUCCESS')                          as num
        from t join dim_payment_method pm using (pm_id)
        where t.txn_ts >= ? and t.txn_ts < ? + interval 1 day
        group by 1
    """, [lo, hi]).fetchall()
    total = sum(r[1] for r in rows)
    return {r[0]: (r[1] / total, r[2] / r[1]) for r in rows}


def main() -> int:
    if not DB.exists():
        print(f"missing {DB} — run data/generate.py first")
        return 1
    con = duckdb.connect(str(DB), read_only=True)
    exp = expected_decomposition()

    print("\n=== 1. NPCI calibration reproduced in the data (UPI only) ===")
    bd, td, appr = con.execute(f"""
        with t as ({DEDUP})
        select 100.0 * count(*) filter (where t.status='FAILED' and fc.decline_class='business')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED')),
               100.0 * count(*) filter (where t.status='FAILED' and fc.decline_class='technical')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED')),
               100.0 * count(*) filter (where t.status='SUCCESS')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED'))
        from t join dim_payment_method pm using (pm_id)
                left join dim_failure_code fc on fc.failure_code = t.failure_code
        where pm.method = 'UPI'
    """).fetchone()
    check("UPI business decline rate", bd, UPI_BUSINESS_DECLINE_PCT, 0.35, "%")
    check("UPI technical decline rate", td, UPI_TECHNICAL_DECLINE_PCT, 0.10, "%")
    check("UPI approval rate", appr, 100 - UPI_BUSINESS_DECLINE_PCT - UPI_TECHNICAL_DECLINE_PCT, 0.35, "%")

    print("\n=== 2. The planted incident: mix-vs-rate decomposition ===")
    base = segments(con, *BASELINE_WEEK)
    targ = segments(con, *TARGET_WEEK)

    base_sr = sum(w * s for w, s in base.values())
    targ_sr = sum(w * s for w, s in targ.values())
    mix = sum((targ[m][0] - base[m][0]) * base[m][1] for m in base)
    rate = sum(targ[m][0] * (targ[m][1] - base[m][1]) for m in base)
    delta = targ_sr - base_sr

    print(f"\n  {'rail':<12} {'share base':>11} {'share tgt':>10} {'SR base':>9} {'SR tgt':>9} {'SR drift':>9}")
    for m in sorted(base, key=lambda x: -base[x][0]):
        print(f"  {m:<12} {base[m][0]*100:>10.2f}% {targ[m][0]*100:>9.2f}% "
              f"{base[m][1]*100:>8.2f}% {targ[m][1]*100:>8.2f}% {(targ[m][1]-base[m][1])*100:>8.2f}pp")

    print()
    check("baseline week success rate", base_sr * 100, exp["baseline_sr_pct"], 0.25, "%")
    check("target week success rate", targ_sr * 100, exp["target_sr_pct"], 0.25, "%")
    check("overall delta", delta * 100, exp["delta_pp"], 0.20)
    check("MIX effect", mix * 100, exp["mix_effect_pp"], 0.20)
    check("RATE effect", rate * 100, exp["rate_effect_pp"], 0.20)
    check("decomposition residual (must be ~0)", (delta - mix - rate) * 100, 0.0, 1e-9)
    share = 100 * mix / delta if delta else 0
    check("share of decline explained by MIX", share, exp["mix_share_pct"], 4.0, "%")

    print("\n=== 3. Per-rail rates are FLAT (the whole point) ===")
    worst = max(abs(targ[m][1] - base[m][1]) * 100 for m in base)
    check("largest per-rail SR drift", worst, 0.10, 0.22)

    print("\n=== 4. Planted defects present ===")
    q = lambda s: con.execute(s).fetchone()[0]
    for label, sql, exp_v, tol in [
        ("duplicate txn_ids", "select count(*) from (select txn_id from fact_transactions group by txn_id having count(*)>1)", 1800, 0),
        ("null bank_id rate", "select 100.0*count(*) filter (where bank_id is null)/count(*) from fact_transactions", 2.3, 0.3),
        ("negative amounts", "select count(*) from fact_transactions where amount<0", 47, 0),
        ("settlement-mismatch merchants", """
            with t as (select merchant_id, count(*) n from fact_transactions
                       where status='SUCCESS' and amount>0 group by 1),
                 s as (select merchant_id, sum(txn_count) n from fact_settlements group by 1)
            select count(*) from t join s using (merchant_id) where abs(t.n-s.n) > 0.02*t.n""", 3, 0),
    ]:
        check(label, float(q(sql)), float(exp_v), tol, "")

    lag = q("select datediff('day', max(txn_ts)::date, (select max(txn_ts)::date from fact_transactions)) from fact_transactions")
    full, partial = con.execute("""
        with d as (select txn_ts::date d, count(*) n from fact_transactions group by 1)
        select median(n) filter (where d < (select max(d) from d) - 1),
               avg(n)    filter (where d >= (select max(d) from d) - 1) from d
    """).fetchone()
    check("recent-day ingest fraction", partial / full, 0.40, 0.08, "")

    print("\n" + "=" * 78)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ALL CHECKS PASSED — data matches the NPCI calibration and the planted incident.")
    print(f"\nHeadline for the demo: success rate fell {abs(delta)*100:.2f}pp "
          f"({base_sr*100:.2f}% → {targ_sr*100:.2f}%), of which {share:.1f}% is TRAFFIC MIX "
          f"and only {abs(rate)*100:.2f}pp is rails actually degrading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
