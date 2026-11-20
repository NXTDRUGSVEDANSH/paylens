"""
PayLens synthetic data generator — calibrated to NPCI published figures.

Synthetic at row level, calibrated at aggregate level. See data/npci_calibration.py
for every constant and its provenance, and datasets/CALIBRATION.md for the writeup.

Determinism
-----------
seed=42, and success counts per (day, method, issuer) cell are allocated by
    n_success = round(n_cell * sr_cell)
rather than by Bernoulli sampling. With ~42k attempts per week the sampling error
on a segment success rate is ~0.15pp, which is larger than the -0.10pp rate effect
this dataset exists to exhibit — random sampling would blur the very signal the
driver engine must detect, and the headline "92.5% of the decline is mix" would
wander between runs. Row-level realism is untouched; only the aggregate is pinned.

Per-method success rates are renormalised after issuer adjustment so each rail's
volume-weighted rate lands exactly on its calibrated target. That keeps the
decomposition arithmetic exact while still giving issuer_bank genuine variation,
so ranking dimensions by explained share is a real operation.

Outputs
-------
    datasets/*.csv        human-inspectable, what a reviewer opens first
    datasets/*.parquet    typed, what the loader actually reads
    data/paylens.duckdb   the queryable database
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from npci_calibration import (
    ACQUIRER_BANKS, FAILURE_CODES, ISSUER_BANKS, METHOD_ISSUER_SENSITIVITY,
    METHOD_MIX_BASE, METHOD_MIX_SHIFTED, METHOD_SUCCESS_RATE, PENDING_RATE,
    SR_DRIFT, UPI_APPROVAL_PCT, calibrate_declines, expected_decomposition,
)

SEED = 42
DAYS = 120
END_DATE = date(2026, 11, 20)
START_DATE = END_DATE - timedelta(days=DAYS - 1)

# Aligned to ISO week boundaries (Mon..Sun) so that a user asking "last week"
# lands exactly on the incident window, with "the week before" as its baseline.
# 2026-11-20 is a Friday, so the last COMPLETE week is Mon 11-09 .. Sun 11-15.
BASELINE_WEEK = (date(2026, 11, 2), date(2026, 11, 8))
TARGET_WEEK = (date(2026, 11, 9), date(2026, 11, 15))
SHIFT_START = TARGET_WEEK[0]

BASE_ATTEMPTS_PER_DAY = 6_000
WEEKDAY_VOLUME = np.array([1.05, 1.08, 1.06, 1.04, 1.12, 0.88, 0.77])  # Mon..Sun
N_MERCHANTS = 240

# ── Planted defects, one per required failure scenario ───────────────────────
DUP_DAY_OFFSET = 83   # double-ingest window -> 2026-10-15, inside the month the
#                       data-quality demo question asks about
DUP_ROWS = 1_800
NULL_BANK_RATE = 0.023
INCOMPLETE_DAYS = 2
INCOMPLETE_FRACTION = 0.40
NEGATIVE_AMOUNT_ROWS = 47
SETTLEMENT_MISMATCH = {"M-1042": 0.070, "M-1108": 0.043, "M-1187": 0.088}

# Netbanking concentrates on public-sector bank pages, which also decline more.
# Makes issuer_bank a genuine *secondary* explanator of the planted incident.
NETBANKING_BIAS = {"B-01": 3.2, "B-06": 2.8, "B-07": 2.4, "B-10": 2.1, "B-08": 1.9}

PM_ROWS = [
    ("PM-01", "UPI",        None,         None,     False),
    ("PM-02", "CARD",       "VISA",       "credit", True),
    ("PM-03", "CARD",       "VISA",       "debit",  False),
    ("PM-04", "CARD",       "MASTERCARD", "credit", True),
    ("PM-05", "CARD",       "RUPAY",      "debit",  False),
    ("PM-06", "NETBANKING", None,         None,     False),
    ("PM-07", "WALLET",     None,         None,     True),
    ("PM-08", "EMI",        "VISA",       "credit", True),
]
PM_BY_METHOD = {"UPI": ["PM-01"], "CARD": ["PM-02", "PM-03", "PM-04", "PM-05"],
                "NETBANKING": ["PM-06"], "WALLET": ["PM-07"], "EMI": ["PM-08"]}
CARD_PM_W = np.array([0.34, 0.28, 0.24, 0.14])

CATEGORIES = ["Groceries", "Electronics", "Fashion", "Travel", "Food Delivery",
              "Utilities", "Pharmacy", "Education", "Fuel", "Entertainment"]
STATE_REGION = {
    "Maharashtra": "West", "Gujarat": "West", "Rajasthan": "West",
    "Karnataka": "South", "Tamil Nadu": "South", "Telangana": "South", "Kerala": "South",
    "Delhi": "North", "Haryana": "North", "Punjab": "North", "Uttar Pradesh": "North",
    "West Bengal": "East", "Odisha": "East", "Bihar": "East", "Assam": "East",
}
CITY = {"Maharashtra": "Mumbai", "Gujarat": "Ahmedabad", "Rajasthan": "Jaipur",
        "Karnataka": "Bengaluru", "Tamil Nadu": "Chennai", "Telangana": "Hyderabad",
        "Kerala": "Kochi", "Delhi": "New Delhi", "Haryana": "Gurugram",
        "Punjab": "Ludhiana", "Uttar Pradesh": "Lucknow", "West Bengal": "Kolkata",
        "Odisha": "Bhubaneswar", "Bihar": "Patna", "Assam": "Guwahati"}


def _h(*p: object) -> int:
    return int(hashlib.md5("|".join(map(str, p)).encode()).hexdigest()[:8], 16)


def _apportion(total: int, weights: np.ndarray) -> np.ndarray:
    """
    Split `total` across `weights` by largest remainder (Hamilton), exactly.

    Used instead of rng.multinomial for the issuer split. Multinomial sampling
    perturbs the issuer mix inside each rail, and because success rate varies by
    issuer that perturbs the rail's own rate — which shows up as a spurious RATE
    effect and corrupts the mix/rate split this dataset exists to demonstrate.
    Deterministic apportionment removes that noise at source.
    """
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()
    exact = w * total
    base = np.floor(exact).astype(int)
    short = total - base.sum()
    if short > 0:
        order = np.argsort(-(exact - base))
        base[order[:short]] += 1
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Success-rate surface: method x issuer, renormalised to hit each rail's target
# ─────────────────────────────────────────────────────────────────────────────
def build_sr_surface(cal: dict) -> tuple[dict, dict]:
    """
    Returns (sr[method][bank_id], bank_weights[method][bank_id]).

    An issuer's own approval rate shifts a rail's rate in proportion to how much
    that rail depends on the issuer (METHOD_ISSUER_SENSITIVITY): UPI rides the
    issuer directly; a pre-funded wallet barely does. Each rail is then shifted
    so its volume-weighted rate equals METHOD_SUCCESS_RATE exactly.
    """
    bank_ids = [b[0] for b in ISSUER_BANKS]
    base_w = np.array([cal[b]["upi_share"] for b in bank_ids])
    base_w = base_w / base_w.sum()
    nb_w = np.array([base_w[i] * NETBANKING_BIAS.get(b, 1.0) for i, b in enumerate(bank_ids)])
    nb_w = nb_w / nb_w.sum()

    upi_mean = UPI_APPROVAL_PCT / 100.0
    sr: dict[str, dict[str, float]] = {}
    weights: dict[str, dict[str, float]] = {}

    for method, (target, _src) in METHOD_SUCCESS_RATE.items():
        w = nb_w if method == "NETBANKING" else base_w
        sens = METHOD_ISSUER_SENSITIVITY[method]
        raw = np.array([target + sens * (cal[b]["approval"] - upi_mean) for b in bank_ids])
        raw = np.clip(raw, 0.30, 0.995)
        raw = raw + (target - float(w @ raw))          # exact renormalisation
        raw = np.clip(raw, 0.30, 0.995)
        sr[method] = dict(zip(bank_ids, raw))
        weights[method] = dict(zip(bank_ids, w))
    return sr, weights


def _weekday_wobble(volumes: np.ndarray) -> np.ndarray:
    """Per-weekday rate wobble, re-centred so its volume-weighted mean is zero."""
    w = np.array([0.0012, 0.0005, -0.0003, -0.0008, -0.0015, 0.0009, 0.0004])
    return w - (volumes @ w) / volumes.sum()


# ─────────────────────────────────────────────────────────────────────────────
# Dimensions
# ─────────────────────────────────────────────────────────────────────────────
def dim_payment_method() -> pd.DataFrame:
    return pd.DataFrame(PM_ROWS, columns=["pm_id", "method", "card_network",
                                          "card_type", "is_tokenized"])


def dim_bank(cal: dict) -> pd.DataFrame:
    rows = [dict(bank_id=b, bank_name=v["name"], bank_type="issuer",
                 ifsc_prefix=v["name"].split()[0][:4].upper(),
                 is_payments_bank=v["is_payments_bank"],
                 npci_business_decline_pct=v["bd"], npci_technical_decline_pct=v["td"])
            for b, v in cal.items()]
    rows += [dict(bank_id=b, bank_name=n, bank_type="acquirer",
                  ifsc_prefix=n.split()[0][:4].upper(), is_payments_bank=False,
                  npci_business_decline_pct=None, npci_technical_decline_pct=None)
             for b, n, _ in ACQUIRER_BANKS]
    return pd.DataFrame(rows)


def dim_merchant() -> pd.DataFrame:
    states = list(STATE_REGION)
    rows = []
    for i in range(N_MERCHANTS):
        mid = f"M-{1000 + i}"
        st = states[_h(mid, "st") % len(states)]
        cat = CATEGORIES[_h(mid, "cat") % len(CATEGORIES)]
        rows.append(dict(
            merchant_id=mid, merchant_name=f"{cat} Store {i + 1:03d}",
            mcc=str(5000 + _h(mid, "mcc") % 900), category=cat,
            city=CITY[st], state=st, region=STATE_REGION[st],
            tier=["enterprise", "mid", "sme"][min(2, _h(mid, "t") % 10 // 3)],
            onboarded_at=START_DATE - timedelta(days=int(_h(mid, "o") % 900) + 30),
            risk_band=["low", "low", "medium", "high"][_h(mid, "r") % 4]))
    return pd.DataFrame(rows)


def dim_failure_code() -> pd.DataFrame:
    return pd.DataFrame(
        [dict(failure_code=c, category=cat, description=d, is_retriable=r,
              owner=o, decline_class=dc) for c, cat, d, r, o, dc, _ in FAILURE_CODES])


# ─────────────────────────────────────────────────────────────────────────────
# Transactions
# ─────────────────────────────────────────────────────────────────────────────
def build_transactions(rng, cal: dict, merchants: pd.DataFrame) -> pd.DataFrame:
    sr_surface, bank_w = build_sr_surface(cal)
    wobble = _weekday_wobble(WEEKDAY_VOLUME)
    bank_ids = [b[0] for b in ISSUER_BANKS]

    mids = merchants["merchant_id"].to_numpy()
    mw = np.where(merchants["tier"].to_numpy() == "enterprise", 3.0,
                  np.where(merchants["tier"].to_numpy() == "mid", 1.6, 1.0))
    mw = mw / mw.sum()

    acq_ids = [a[0] for a in ACQUIRER_BANKS]
    acq_w = np.array([a[2] for a in ACQUIRER_BANKS])

    f_business = [f[0] for f in FAILURE_CODES if f[5] == "business"]
    f_bus_w = np.array([f[6] for f in FAILURE_CODES if f[5] == "business"]); f_bus_w /= f_bus_w.sum()
    f_technical = [f[0] for f in FAILURE_CODES if f[5] == "technical"]
    f_tec_w = np.array([f[6] for f in FAILURE_CODES if f[5] == "technical"]); f_tec_w /= f_tec_w.sum()

    lat_base = {"UPI": 900, "CARD": 1900, "NETBANKING": 3400, "WALLET": 1100, "EMI": 2600}
    chunks: list[pd.DataFrame] = []
    seq = 0

    for d in range(DAYS):
        day = START_DATE + timedelta(days=d)
        wd = day.weekday()
        shifted = day >= SHIFT_START
        mix = METHOD_MIX_SHIFTED if shifted else METHOD_MIX_BASE
        drift = SR_DRIFT if shifted else 0.0

        n_day = int(BASE_ATTEMPTS_PER_DAY * WEEKDAY_VOLUME[wd] * (1.0 + 0.0015 * d))
        if (END_DATE - day).days < INCOMPLETE_DAYS:
            n_day = int(n_day * INCOMPLETE_FRACTION)     # planted: partial ingest

        for method, share in mix.items():
            n_m = int(round(n_day * share))
            if n_m <= 0:
                continue
            w = np.array([bank_w[method][b] for b in bank_ids])
            counts = _apportion(n_m, w)

            for bi, n_cell in enumerate(counts):
                if n_cell <= 0:
                    continue
                bid = bank_ids[bi]
                sr = float(np.clip(sr_surface[method][bid] + drift + wobble[wd], 0.05, 0.999))
                n_pend = int(round(n_cell * PENDING_RATE))
                n_res = n_cell - n_pend
                n_succ = int(round(n_res * sr))          # deterministic — see docstring
                n_fail = n_res - n_succ

                status = np.array(["SUCCESS"] * n_succ + ["FAILED"] * n_fail
                                  + ["PENDING"] * n_pend, dtype=object)
                rng.shuffle(status)

                # Failure class follows NPCI's split: technical declines are rare.
                td_share = cal[bid]["td"] / max(cal[bid]["bd"] + cal[bid]["td"], 1e-9)
                is_tech = rng.random(n_cell) < td_share
                fcode = np.where(
                    status == "FAILED",
                    np.where(is_tech, rng.choice(f_technical, n_cell, p=f_tec_w),
                             rng.choice(f_business, n_cell, p=f_bus_w)), None)

                pm = (rng.choice(PM_BY_METHOD[method], n_cell, p=CARD_PM_W)
                      if method == "CARD" else np.full(n_cell, PM_BY_METHOD[method][0]))
                # Unattributed issuer, only on rails that genuinely may lack one.
                null_bank = (rng.random(n_cell) < NULL_BANK_RATE / 0.67
                             if method in ("UPI", "WALLET") else np.zeros(n_cell, bool))
                secs = rng.integers(0, 86400, n_cell)
                ts = np.array([datetime(day.year, day.month, day.day)
                               + timedelta(seconds=int(s)) for s in secs])
                retry = rng.random(n_cell) < 0.055
                ids = np.arange(seq + 1, seq + 1 + n_cell)
                seq += n_cell

                chunks.append(pd.DataFrame(dict(
                    txn_id=[f"T-{i:08d}" for i in ids],
                    txn_ts=ts,
                    merchant_id=rng.choice(mids, n_cell, p=mw),
                    bank_id=np.where(null_bank, None, bid),
                    acquirer_bank_id=rng.choice(acq_ids, n_cell, p=acq_w),
                    pm_id=pm,
                    terminal_id=[f"TERM-{_h(i, 'tm') % 9000 + 1000}" for i in ids],
                    amount=np.round(np.exp(rng.normal(6.6, 0.95, n_cell)), 2),
                    currency="INR",
                    status=status,
                    failure_code=fcode,
                    latency_ms=np.maximum(80, rng.normal(
                        lat_base[method], lat_base[method] * 0.3, n_cell)).astype(int),
                    is_retry=retry,
                    original_txn_id=np.where(
                        retry, [f"T-{max(1, i - (_h(i, 'o') % 50 + 1)):08d}" for i in ids], None),
                    settlement_id=None,
                    ingested_at=pd.DatetimeIndex(ts) + pd.to_timedelta(
                        np.array([_h(i, "ing") % 120 + 5 for i in ids]), unit="m"),
                )))

    return pd.concat(chunks, ignore_index=True)


def plant_duplicates(df: pd.DataFrame, rng) -> tuple[pd.DataFrame, int]:
    """Double-ingest window: exact duplicate txn_ids differing only in ingested_at."""
    day = START_DATE + timedelta(days=DUP_DAY_OFFSET)
    mask = df["txn_ts"].dt.date == day
    idx = df.index[mask]
    if len(idx) == 0:
        return df, 0
    pick = rng.choice(idx, size=min(DUP_ROWS, len(idx)), replace=False)
    dup = df.loc[pick].copy()
    dup["ingested_at"] = dup["ingested_at"] + pd.Timedelta(hours=6)
    return pd.concat([df, dup], ignore_index=True), len(pick)


def plant_negative_amounts(df: pd.DataFrame, rng) -> int:
    """Refunds mislabelled as payments."""
    cand = df.index[(df["status"] == "SUCCESS") & (df["amount"] > 100)]
    pick = rng.choice(cand, size=min(NEGATIVE_AMOUNT_ROWS, len(cand)), replace=False)
    df.loc[pick, "amount"] = -df.loc[pick, "amount"].abs()
    return len(pick)


def build_settlements(df: pd.DataFrame) -> pd.DataFrame:
    """
    One batch per merchant per day. txn_count is deliberately UNDERCOUNTED for three
    merchants, so two sources disagree on the same metric and the precedence rule in
    semantic_model.yaml must be applied and explained rather than silently chosen.
    """
    ok = df[(df["status"] == "SUCCESS") & (df["amount"] > 0)].copy()
    ok["d"] = ok["txn_ts"].dt.date
    g = ok.groupby(["merchant_id", "d"], as_index=False).agg(
        txn_count=("txn_id", "size"), gross_amount=("amount", "sum"))
    # Apply the shortfall on each merchant's TOTAL, then apportion it back across
    # days. Rounding per day silently erased the gap for low-volume merchants,
    # which made the "two sources disagree" scenario untestable.
    g["txn_count"] = g["txn_count"].astype(int)
    for mid, pct in SETTLEMENT_MISMATCH.items():
        rows = g.index[g["merchant_id"] == mid]
        if len(rows) == 0:
            raise ValueError(f"SETTLEMENT_MISMATCH references unknown merchant {mid}")
        daily = g.loc[rows, "txn_count"].to_numpy()
        target = int(round(daily.sum() * (1 - pct)))
        g.loc[rows, "txn_count"] = _apportion(target, daily)
    g["gross_amount"] = g["gross_amount"].round(2)
    g["fees"] = (g["gross_amount"] * 0.0195).round(2)
    g["tax"] = (g["fees"] * 0.18).round(2)
    g["net_amount"] = (g["gross_amount"] - g["fees"] - g["tax"]).round(2)
    g["settlement_id"] = [f"S-{_h(m, d):08x}" for m, d in zip(g["merchant_id"], g["d"])]
    g["settlement_date"] = [d + timedelta(days=2) for d in g["d"]]
    g["status"] = ["SETTLED" if (END_DATE - d).days > 3 else "PENDING" for d in g["d"]]
    return g[["settlement_id", "merchant_id", "settlement_date", "txn_count",
              "gross_amount", "fees", "tax", "net_amount", "status"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--db", default=str(root / "data" / "paylens.duckdb"))
    ap.add_argument("--datasets", default=str(root / "datasets"))
    args = ap.parse_args()

    rng = np.random.default_rng(SEED)
    cal = calibrate_declines()
    print(f"PayLens generator — {START_DATE} .. {END_DATE} ({DAYS} days), seed={SEED}")
    print(f"calibrated to NPCI: UPI approval {UPI_APPROVAL_PCT:.2f}% "
          f"(BD {100 - UPI_APPROVAL_PCT - 0.24:.2f}% + TD 0.24%)")

    pms, banks, merchants, fcodes = (dim_payment_method(), dim_bank(cal),
                                     dim_merchant(), dim_failure_code())
    txns = build_transactions(rng, cal, merchants)
    print(f"  clean attempts       {len(txns):>9,}")
    txns, n_dup = plant_duplicates(txns, rng)
    n_neg = plant_negative_amounts(txns, rng)
    setts = build_settlements(txns)
    print(f"  with duplicates      {len(txns):>9,}")
    print(f"  settlement batches   {len(setts):>9,}")

    tables = {"dim_payment_method": pms, "dim_bank": banks, "dim_merchant": merchants,
              "dim_failure_code": fcodes, "fact_transactions": txns,
              "fact_settlements": setts}

    ds = Path(args.datasets)
    ds.mkdir(parents=True, exist_ok=True)
    CSV_FULL_MAX = 100_000      # keep the repo cloneable
    for name, frame in tables.items():
        frame.to_parquet(ds / f"{name}.parquet", index=False)
        if len(frame) <= CSV_FULL_MAX:
            frame.to_csv(ds / f"{name}.csv", index=False)
        else:
            # Full CSV of fact_transactions is ~91MB. Parquet holds everything;
            # the CSV sample is for a human to eyeball the columns.
            frame.head(5_000).to_csv(ds / f"{name}.sample.csv", index=False)

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    con = duckdb.connect(str(db))
    for name, frame in tables.items():
        con.register(f"_{name}", frame)
        con.execute(f"create table {name} as select * from _{name}")
    con.execute("checkpoint")

    q = lambda s: con.execute(s).fetchone()[0]
    print("\n── Planted defects (each maps to a required failure scenario) ──")
    print(f"  1 duplicate txn_ids        {q('select count(*) from (select txn_id from fact_transactions group by txn_id having count(*)>1)'):>9,}  (+{n_dup:,} rows)")
    print(f"  2 null bank_id             {q('select count(*) from fact_transactions where bank_id is null'):>9,}  ({q('select round(100.0*count(*) filter (where bank_id is null)/count(*),2) from fact_transactions')}%)")
    print(f"  3 settlement disagreement  {len(SETTLEMENT_MISMATCH):>9}  {', '.join(SETTLEMENT_MISMATCH)}")
    print(f"  4 negative amounts         {q('select count(*) from fact_transactions where amount<0'):>9,}")
    print(f"  5 partial ingest (days)    {INCOMPLETE_DAYS:>9}  (~{int(INCOMPLETE_FRACTION*100)}% of expected volume)")
    print(f"  6 mix shift from {SHIFT_START}  netbanking 8% → 15%")

    print("\n── Row counts ──")
    for t in tables:
        print(f"  {t:<22} {q(f'select count(*) from {t}'):>9,}")
    con.close()
    print(f"\nduckdb   {db}")
    print(f"datasets {ds}  (csv + parquet)")
    print("\nexpected decomposition (from calibration constants):")
    for k, v in expected_decomposition().items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
