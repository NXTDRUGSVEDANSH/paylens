"""
Data-quality gate. Runs against the tables a query actually touched, and attaches
typed caveats to the answer rather than hiding them on a separate screen.

Every probe goes through the guard like any user query — invariant 1 has no
exception for background checks, which is the easiest place to leak data.

The reconciliation check is the interesting one: when two sources disagree it
applies the DOCUMENTED precedence rule from semantic_model.yaml and quantifies the
gap, rather than silently picking a number.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable

from backend.core.schemas import Caveat, TimeWindow
from semantic.loader import SemanticModel

log = logging.getLogger("paylens.dq")

Runner = Callable[[str], list[dict]]


def _msg(model: SemanticModel, check_id: str, **kw) -> str:
    for c in model.data_quality["checks"]:
        if c["id"] == check_id:
            return " ".join(str(c["message"]).split()).format(**kw)
    return check_id


def _cfg(model: SemanticModel, check_id: str) -> dict:
    for c in model.data_quality["checks"]:
        if c["id"] == check_id:
            return c
    return {}


def _win(w: TimeWindow) -> tuple[str, str]:
    return w.start.isoformat(), (w.end + timedelta(days=1)).isoformat()


def check_duplicates(run: Runner, model: SemanticModel, w: TimeWindow) -> Caveat | None:
    lo, hi = _win(w)
    rows = run(f"""
        SELECT count(*) AS dup_groups, coalesce(sum(n) - count(*), 0) AS extra_rows
        FROM (SELECT txn_id, count(*) AS n FROM fact_transactions
              WHERE txn_ts >= '{lo}' AND txn_ts < '{hi}'
              GROUP BY txn_id HAVING count(*) > 1)
    """)
    if not rows or not rows[0]["dup_groups"]:
        return None
    n = int(rows[0]["extra_rows"])
    return Caveat(
        id="duplicate_pk", severity="warn", dataset="fact_transactions",
        message=_msg(model, "duplicate_pk", n=f"{n:,}", pk="txn_id"),
        evidence={"duplicate_groups": int(rows[0]["dup_groups"]), "extra_rows": n,
                  "resolution": "de-duplicated by latest ingested_at"})


def check_null_dimension(run: Runner, model: SemanticModel, w: TimeWindow) -> Caveat | None:
    cfg = _cfg(model, "null_issuer")
    col = cfg.get("column", "bank_id")
    thresh = float(cfg.get("threshold", 0.005))
    lo, hi = _win(w)
    rows = run(f"""
        SELECT count(*) AS total, count(*) FILTER (WHERE {col} IS NULL) AS nulls
        FROM fact_transactions WHERE txn_ts >= '{lo}' AND txn_ts < '{hi}'
    """)
    if not rows or not rows[0]["total"]:
        return None
    frac = rows[0]["nulls"] / rows[0]["total"]
    if frac < thresh:
        return None
    return Caveat(
        id="null_issuer", severity="warn", dataset="fact_transactions",
        message=_msg(model, "null_issuer", pct=f"{frac * 100:.2f}%"),
        evidence={"null_rows": int(rows[0]["nulls"]), "total_rows": int(rows[0]["total"]),
                  "null_pct": round(frac * 100, 2),
                  "effect": "segment rows will not sum to the overall total"})


def check_freshness(run: Runner, model: SemanticModel, w: TimeWindow
                    ) -> tuple[Caveat | None, TimeWindow]:
    """
    Detect partially-ingested recent days and TRIM the window rather than letting
    them fake a downward trend. Returns the caveat and the window to actually use.
    """
    lag = model.datasets["fact_transactions"].freshness_lag_days
    if lag <= 0:
        return None, w
    rows = run("""
        SELECT txn_ts::DATE AS d, count(*) AS n FROM fact_transactions
        GROUP BY 1 ORDER BY 1
    """)
    if len(rows) < 10:
        return None, w
    counts = [r["n"] for r in rows]
    typical = sorted(counts[:-lag])[len(counts[:-lag]) // 2]
    recent = [r for r in rows[-lag:] if date.fromisoformat(str(r["d"])) <= w.end]
    if not recent:
        return None, w
    avg = sum(r["n"] for r in recent) / len(recent)
    if avg >= 0.85 * typical:
        return None, w

    trimmed_end = w.end - timedelta(days=len(recent))
    if trimmed_end < w.start:
        trimmed_end = w.end
    new_w = TimeWindow(start=w.start, end=trimmed_end)
    return Caveat(
        id="incomplete_window", severity="warn", dataset="fact_transactions",
        message=_msg(model, "incomplete_window", lag_days=len(recent),
                     pct=f"{avg / typical * 100:.0f}%", trimmed_to=trimmed_end.isoformat()),
        evidence={"typical_daily_rows": int(typical), "recent_daily_avg": int(avg),
                  "trimmed_from": w.end.isoformat(), "trimmed_to": trimmed_end.isoformat()},
    ), new_w


def check_ranges(run: Runner, model: SemanticModel, w: TimeWindow) -> Caveat | None:
    lo, hi = _win(w)
    rows = run(f"""
        SELECT count(*) AS n FROM fact_transactions
        WHERE txn_ts >= '{lo}' AND txn_ts < '{hi}' AND amount < 0
    """)
    n = int(rows[0]["n"]) if rows else 0
    if not n:
        return None
    return Caveat(id="negative_amount", severity="info", dataset="fact_transactions",
                  message=_msg(model, "negative_amount", n=f"{n:,}"),
                  evidence={"rows": n, "effect": "excluded from GMV and average ticket"})


def reconcile(run: Runner, model: SemanticModel, w: TimeWindow,
              merchant_ids: list[str] | None = None) -> Caveat | None:
    """
    Two sources, one metric. Applies the documented precedence rule and quantifies
    the gap — never averages them, never silently picks the larger.
    """
    rule = next((r for r in model.data_quality["reconciliation"]
                 if r["metric"] == "txn_count"), None)
    if not rule:
        return None
    lo, hi = _win(w)
    mfilter = ""
    if merchant_ids:
        quoted = ", ".join(f"'{m}'" for m in merchant_ids)
        mfilter = f"AND merchant_id IN ({quoted})"
    rows = run(f"""
        WITH t AS (
          SELECT merchant_id, count(*) AS n FROM (
            SELECT * FROM fact_transactions
            QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY ingested_at DESC) = 1)
          WHERE txn_ts >= '{lo}' AND txn_ts < '{hi}' AND status = 'SUCCESS' AND amount > 0
          {mfilter} GROUP BY 1),
        s AS (
          SELECT merchant_id, sum(txn_count) AS n FROM fact_settlements
          WHERE settlement_date >= '{lo}' AND settlement_date < '{hi}' {mfilter}
          GROUP BY 1)
        SELECT t.merchant_id, t.n AS txn_n, s.n AS settled_n,
               round(100.0 * (t.n - s.n) / nullif(t.n, 0), 2) AS gap_pct
        FROM t JOIN s USING (merchant_id)
        WHERE abs(t.n - s.n) > 0.02 * t.n
        ORDER BY abs(t.n - s.n) DESC LIMIT 5
    """)
    if not rows:
        return None
    worst = rows[0]
    return Caveat(
        id="settlement_mismatch", severity="warn", dataset="fact_settlements",
        message=_msg(model, "settlement_mismatch",
                     settled_count=f"{int(worst['settled_n']):,}",
                     merchant=worst["merchant_id"],
                     txn_count=f"{int(worst['txn_n']):,}",
                     delta_pct=f"{worst['gap_pct']:+.1f}%"),
        evidence={"affected_merchants": [r["merchant_id"] for r in rows],
                  "worst": worst, "precedence": rule["precedence"],
                  "reason": " ".join(rule["reason"].split())})


def run_checks(run: Runner, model: SemanticModel, w: TimeWindow, tables: list[str],
               merchant_ids: list[str] | None = None
               ) -> tuple[list[Caveat], TimeWindow]:
    """All applicable checks for the tables this query touched."""
    caveats: list[Caveat] = []
    window = w
    if "fact_transactions" in tables:
        fresh, window = check_freshness(run, model, w)
        if fresh:
            caveats.append(fresh)
        for fn in (check_duplicates, check_null_dimension, check_ranges):
            try:
                c = fn(run, model, window)
                if c:
                    caveats.append(c)
            except Exception as exc:  # noqa: BLE001
                log.warning("DQ check %s failed: %s", fn.__name__, exc)
    if "fact_settlements" in tables or merchant_ids:
        try:
            c = reconcile(run, model, window, merchant_ids)
            if c:
                caveats.append(c)
        except Exception as exc:  # noqa: BLE001
            log.warning("reconciliation failed: %s", exc)
    order = {"block": 0, "warn": 1, "info": 2}
    caveats.sort(key=lambda c: order[c.severity])
    return caveats, window
