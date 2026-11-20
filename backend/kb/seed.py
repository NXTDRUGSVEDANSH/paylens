"""
Seed the knowledge base from semantic/seeds.yaml.

Idempotent via a version marker, like Fractal AI's `_KB_VERSION` gate, but keyed on
the SEMANTIC MODEL VERSION — so editing the model forces a reseed instead of leaving
stale records that reference renamed columns.

Only questions whose SQL actually executes and returns rows are seeded: a broken
seed would otherwise be served at tier 1 with zero LLM calls and nothing to catch it.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml

from backend import config
from backend.core import rbac
from backend.core.compiler import bind, compile_plan
from backend.core.executor import execute
from backend.core.guard import guard
from backend.core.planner import plan_from_resolution
from backend.core.resolver import resolve
from backend.kb import store
from backend.kb.keys import record_id
from backend.kb.store import KBRecord
from semantic.loader import SemanticModel

log = logging.getLogger("paylens.kb.seed")
MARKER_ID = "__seed_marker__"


def _marker_meta(model: SemanticModel) -> dict:
    return {"shape_key": "__marker__", "model_version": model.version,
            "prompt_version": config.PROMPT_VERSION, "verified": False,
            "healed": False, "source": "marker", "role_agnostic": True,
            "param_arity": -1, "created_at": int(time.time())}


def already_seeded(model: SemanticModel) -> bool:
    rec = store.get(MARKER_ID)
    return bool(rec and rec.meta.get("model_version") == model.version)


def seed(model: SemanticModel, *, path: Path | None = None, force: bool = False
         ) -> dict:
    if not store.available():
        return {"seeded": 0, "skipped": 0, "failed": 0, "reason": "KB unavailable"}
    if already_seeded(model) and not force:
        return {"seeded": 0, "skipped": 0, "failed": 0,
                "reason": f"already seeded at {model.version}"}

    src = yaml.safe_load((path or config.SEEDS_PATH).read_text(encoding="utf-8"))
    cxo = rbac.principal_for("cxo")
    records: list[KBRecord] = []
    failed: list[tuple[str, str]] = []

    for item in src["questions"]:
        q = item["q"]
        try:
            res = resolve(q, model)
            if not res.metrics:
                failed.append((q, "no metric resolved"))
                continue
            plan = plan_from_resolution(res, model)
            cq = compile_plan(plan, model)
            g = guard(bind(cq.template_sql, cq.params), cxo, model)
            out = execute(g.sql)
            if not out.rows:
                failed.append((q, "returned zero rows"))
                continue
            records.append(KBRecord(
                record_id=record_id(res.shape, res.canonical_question),
                canonical_question=res.canonical_question, raw_question=q,
                template_sql=cq.template_sql, plan=plan.model_dump(mode="json"),
                param_schema={k: v.type for k, v in cq.params.items()},
                meta={
                    "shape_key": res.shape, "model_version": model.version,
                    "prompt_version": config.PROMPT_VERSION,
                    "verified": True, "healed": False,
                    "source": "seed", "role_agnostic": True, "invalid_reason": "",
                    "created_at": int(time.time()), "last_used_at": 0,
                    "use_count": 0, "success_count": 1,
                    "golden_id": "", "intent": plan.intent,
                    "metrics_csv": ",".join([plan.metric, *plan.secondary_metrics]),
                    "dims_csv": ",".join(plan.segment_by),
                    "filter_dims_csv": ",".join(f.dimension for f in plan.filters),
                    "param_names_csv": ",".join(sorted(cq.params)),
                    "param_arity": len(cq.params),
                    "is_aggregate": cq.is_aggregate,
                    "time_grain": plan.time_grain or "",
                    "row_count": len(out.rows), "exec_ms": out.exec_ms,
                    "is_demo": bool(item.get("demo", False)),
                }))
        except Exception as exc:  # noqa: BLE001
            failed.append((q, str(exc)[:160]))

    store.upsert_many(records)
    store.upsert(KBRecord(record_id=MARKER_ID, canonical_question="__marker__",
                          raw_question="", template_sql="", plan={}, param_schema={},
                          meta=_marker_meta(model)))
    for q, why in failed:
        log.warning("seed skipped %r: %s", q[:70], why)
    return {"seeded": len(records), "failed": len(failed),
            "failures": failed, "model_version": model.version}
