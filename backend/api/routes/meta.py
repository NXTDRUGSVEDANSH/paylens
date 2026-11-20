"""Metadata endpoints: what the system knows, and who may ask what."""
from __future__ import annotations

from fastapi import APIRouter

from backend.core import db
from semantic.loader import get_model

router = APIRouter(tags=["meta"])


@router.get("/datasets")
def datasets() -> dict:
    """Serves the brief's step 2: what datasets, measures and dimensions exist."""
    m = get_model()
    cols = db.table_columns()
    return {
        "version": m.version,
        "datasets": [
            {"id": d.id, "label": d.label, "grain": d.grain,
             "primary_key": d.primary_key, "time_column": d.time_column,
             "freshness_lag_days": d.freshness_lag_days,
             "description": d.description, "columns": sorted(cols.get(d.id, []))}
            for d in m.datasets.values()],
        "metrics": [
            {"id": x.id, "label": x.label, "type": x.type, "base": x.base,
             "format": x.format, "decomposable": x.decomposable,
             "numerator": x.numerator, "denominator": x.denominator,
             "denominator_note": " ".join((x.denominator_note or "").split()),
             "expr": x.expr, "synonyms": list(x.synonyms)}
            for x in m.metrics.values()],
        "dimensions": [
            {"id": d.id, "label": d.label, "table": d.table, "column": d.column,
             "decomposable": d.decomposable, "nullable": d.nullable,
             "access_scoped": d.rbac_scope, "synonyms": list(d.synonyms)}
            for d in m.dimensions.values()],
        "intents": m.intents,
        "time_grains": sorted(m.time_grains),
    }


@router.get("/roles")
def roles() -> dict:
    m = get_model()
    return {"roles": [
        {"key": r.key, "label": r.label,
         "row_scope": r.row_scope,
         "masked_columns": list(r.masked_columns),
         "denied_metrics": list(r.denied_metrics),
         "required_claim": (r.row_scope or {}).get("claim")}
        for r in m.roles.values()]}


@router.get("/suggestions")
def suggestions(role: str = "cxo") -> dict:
    """The demo script, exposed so the UI can offer one-click questions."""
    common = [
        {"q": "Why did our overall success rate drop last week?",
         "why": "mix-vs-rate decomposition — the headline capability"},
        {"q": "How many transactions did merchant M-1042 process in October?",
         "why": "triggers duplicate, null-issuer and settlement caveats"},
        {"q": "What's our success rate?",
         "why": "ambiguous window and denominator — answered under stated assumptions"},
        {"q": "Success rate by issuer bank last week",
         "why": "NPCI-calibrated per-bank decline rates"},
        {"q": "Show me every single transaction row ever",
         "why": "row-cap control on a detail listing"},
        {"q": "Delete all failed transactions",
         "why": "read-only refusal"},
    ]
    if role == "merchant_user":
        common = [
            {"q": "What is my success rate last week?", "why": "scoped to your merchant"},
            {"q": "Compare my success rate with merchant M-2201",
             "why": "out-of-scope entity — explicit refusal"},
            {"q": "My success rate by payment method last week",
             "why": "your own traffic, broken down"},
        ]
    return {"role": role, "suggestions": common}


@router.get("/calibration")
def calibration() -> dict:
    """Per-bank NPCI decline rates, queried from the data rather than a doc."""
    rows = db.fetch_dicts("""
        SELECT bank_name, is_payments_bank,
               npci_business_decline_pct AS business_decline_pct,
               npci_technical_decline_pct AS technical_decline_pct,
               round(100 - npci_business_decline_pct - npci_technical_decline_pct, 2)
                 AS implied_approval_pct
        FROM dim_bank WHERE bank_type = 'issuer'
        ORDER BY npci_business_decline_pct
    """)
    return {"source": "NPCI BD/TD statistics, September 2026 (see datasets/CALIBRATION.md)",
            "note": ("Aggregate and the three named outliers are published NPCI figures; "
                     "the interior distribution is modelled and scaled to reproduce them."),
            "banks": rows}
