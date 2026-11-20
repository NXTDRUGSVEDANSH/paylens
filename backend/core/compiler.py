"""
Deterministic plan -> SQL compiler.

The LLM produces a validated AnalysisPlan; this module turns it into SQL. Because
the plan is structured and the metric definitions come from the semantic model, the
SQL is generated rather than written — no syntax errors, no hallucinated columns,
and nothing to self-heal on the happy path.

What comes out is a TEMPLATE: pre-RBAC, pre-LIMIT, with named `:placeholders`.
That is what the knowledge base stores, so one record serves any time window and
carries no tenant's filter. Binding happens immediately before the guard.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import sqlglot
from sqlglot import exp

from backend.core.schemas import AnalysisPlan, CompiledQuery, Param
from semantic.loader import Dimension, SemanticModel

PERIOD_BASELINE = "baseline"
PERIOD_TARGET = "target"


def _esc(v: str) -> str:
    return str(v).replace("'", "''")


def bind(template_sql: str, params: dict[str, Param]) -> str:
    """
    Substitute `:name` placeholders with properly escaped literals.

    Reuse requires the placeholder set to equal the bound parameter set EXACTLY —
    an unbound placeholder or an unused binding is a cache miss, not a best-effort
    fill. That is what turns "last week" vs "last month" from a silent collision
    into a parameter substitution.
    """
    found = set(re.findall(r":([a-z_][a-z0-9_]*)", template_sql))
    given = set(params)
    if found != given:
        raise ValueError(
            f"parameter mismatch — template needs {sorted(found)}, got {sorted(given)}")

    def lit(p: Param) -> str:
        if p.type in ("date", "timestamp"):
            return f"'{p.value}'"
        if p.type == "string":
            return f"'{_esc(p.value)}'"
        if p.type == "list":
            if not p.value:
                return "(NULL)"
            return "(" + ", ".join(f"'{_esc(x)}'" for x in p.value) + ")"
        return str(p.value)

    out = template_sql
    for name in sorted(found, key=len, reverse=True):   # longest first: :win_end before :win
        out = re.sub(rf":{name}\b", lit(params[name]), out)
    return out


def _dedupe_cte(model: SemanticModel, table: str) -> tuple[str, str]:
    """(cte_sql, cte_name). QUALIFY applies the model's documented dedupe rule."""
    ds = model.datasets[table]
    name = "src"
    if ds.dedupe_rule:
        return (f"{name} AS (\n  SELECT * FROM {table}\n  QUALIFY {ds.dedupe_rule}\n)", name)
    return (f"{name} AS (\n  SELECT * FROM {table}\n)", name)


def _dim_alias(d: Dimension) -> str:
    return {"dim_payment_method": "pm", "dim_bank": "ib", "dim_merchant": "mer",
            "dim_failure_code": "fc"}.get(d.table, d.table[:3])


def _joins_for(model: SemanticModel, base: str, dims: list[Dimension]) -> list[str]:
    seen: dict[str, str] = {}
    out: list[str] = []
    for d in dims:
        if d.table == base or d.table in seen:
            continue
        j = model.join_path(base, d.table)
        if j is None:
            raise ValueError(f"no join path from {base} to {d.table} in the semantic model")
        a = _dim_alias(d)
        seen[d.table] = a
        out.append(f"LEFT JOIN {d.table} {a} ON {a}.{j.right_on} = t.{j.left_on}")
    return out


def _dim_expr(d: Dimension, base: str) -> str:
    # A dimension whose table IS the fact table is already in scope as `t`.
    ref = f"t.{d.column}" if d.table == base else f"{_dim_alias(d)}.{d.column}"
    if d.nullable and d.null_label:
        return f"COALESCE({ref}, '{_esc(d.null_label)}')"
    return ref


def compile_plan(plan: AnalysisPlan, model: SemanticModel) -> CompiledQuery:
    metric = model.metric(plan.metric)
    base = metric.base
    ds = model.datasets[base]
    tcol = ds.time_column
    if not tcol:
        raise ValueError(f"dataset {base} has no time column; cannot window it")

    dims = [model.dimension(d) for d in plan.segment_by]
    filter_dims = [model.dimension(f.dimension) for f in plan.filters]
    cte, cte_name = _dedupe_cte(model, base)

    params: dict[str, Param] = {
        "win_start": Param(name="win_start", type="date", value=plan.window.start.isoformat()),
        "win_end": Param(name="win_end", type="date",
                         value=(plan.window.end + timedelta(days=1)).isoformat()),
    }

    # ── detail intent: raw rows, no aggregation ─────────────────────────────
    # This is the ONLY intent allowed to truncate instead of block, so it must
    # genuinely produce row-level output rather than a one-row aggregate.
    if plan.intent == "detail":
        return _compile_detail(plan, model, base, tcol, cte, cte_name, params, dims,
                               filter_dims)

    select: list[str] = []
    group: list[str] = []
    segment_columns: list[str] = []

    # Two windows in one query: everything the decomposition needs, one scan.
    if plan.baseline:
        params["base_start"] = Param(name="base_start", type="date",
                                     value=plan.baseline.start.isoformat())
        params["base_end"] = Param(name="base_end", type="date",
                                   value=(plan.baseline.end + timedelta(days=1)).isoformat())
        select.append(
            f"CASE WHEN t.{tcol} >= :win_start AND t.{tcol} < :win_end THEN '{PERIOD_TARGET}'"
            f" WHEN t.{tcol} >= :base_start AND t.{tcol} < :base_end THEN '{PERIOD_BASELINE}'"
            f" END AS period")
        group.append("period")
        segment_columns.append("period")

    if plan.time_grain:
        grain = model.time_grains[plan.time_grain]["expr"].replace("{col}", f"t.{tcol}")
        select.append(f"{grain} AS bucket")
        group.append("bucket")
        segment_columns.append("bucket")

    for d in dims:
        select.append(f"{_dim_expr(d, base)} AS {d.id}")
        group.append(d.id)
        segment_columns.append(d.id)

    # Ratio metrics emit numerator and denominator separately, so Python can
    # re-aggregate correctly. Averaging a ratio of ratios is the classic error.
    value_columns: list[str] = []
    if metric.is_ratio:
        select.append(f"{metric.numerator} AS _num")
        select.append(f"{metric.denominator} AS _den")
        select.append(f"CASE WHEN ({metric.denominator}) > 0 "
                      f"THEN ({metric.numerator})::DOUBLE / ({metric.denominator}) END AS {metric.id}")
        value_columns = ["_num", "_den", metric.id]
    else:
        select.append(f"{metric.expr} AS {metric.id}")
        value_columns = [metric.id]
    for sm in plan.secondary_metrics:
        m2 = model.metric(sm)
        if m2.is_ratio:
            select.append(f"CASE WHEN ({m2.denominator}) > 0 "
                          f"THEN ({m2.numerator})::DOUBLE / ({m2.denominator}) END AS {m2.id}")
        else:
            select.append(f"{m2.expr} AS {m2.id}")
        value_columns.append(m2.id)
    # Volume always travels with a ratio: a rate on 12 attempts is not news.
    if metric.is_ratio and "txn_count" not in plan.secondary_metrics:
        select.append("count(*) AS attempts")
        value_columns.append("attempts")

    where: list[str] = []
    # Dimensions that are only defined on a subset of rows bring their predicate
    # with them (e.g. a failure code exists only on a FAILED attempt).
    for d in dims + filter_dims:
        if d.implies_filter and d.implies_filter not in where:
            where.append(d.implies_filter)
    if plan.baseline:
        where.append(f"((t.{tcol} >= :win_start AND t.{tcol} < :win_end)"
                     f" OR (t.{tcol} >= :base_start AND t.{tcol} < :base_end))")
    else:
        where.append(f"t.{tcol} >= :win_start AND t.{tcol} < :win_end")

    for i, f in enumerate(plan.filters):
        d = model.dimension(f.dimension)
        pname = f"f{i}"
        if f.op in ("in", "not in"):
            params[pname] = Param(name=pname, type="list", value=list(f.values))
            where.append(f"{_dim_expr(d, base)} {f.op.upper()} :{pname}")
        else:
            v = f.values[0] if f.values else None
            params[pname] = Param(name=pname, type="string", value=v)
            where.append(f"{_dim_expr(d, base)} {f.op} :{pname}")

    is_aggregate = model.is_aggregate_intent(plan.intent)
    sql = ["WITH " + cte, "SELECT", "  " + ",\n  ".join(select),
           f"FROM {cte_name} t"]
    sql += ["  " + j for j in _joins_for(model, base, dims + filter_dims)]
    sql.append("WHERE " + "\n  AND ".join(where))
    if is_aggregate and group:
        sql.append("GROUP BY " + ", ".join(group))
    elif is_aggregate and not group:
        pass                                    # single aggregate row
    if plan.intent == "rank" and dims:
        sql.append(f"ORDER BY {metric.id} DESC NULLS LAST")
    elif plan.time_grain:
        sql.append("ORDER BY bucket")
    elif group:
        sql.append(f"ORDER BY {'attempts' if metric.is_ratio else metric.id} DESC NULLS LAST")
    if plan.top_n and plan.intent == "rank":
        sql.append(f"LIMIT {int(plan.top_n)}")

    return CompiledQuery(
        template_sql="\n".join(sql), params=params,
        tables=sorted({base} | {d.table for d in dims + filter_dims}),
        is_aggregate=is_aggregate, value_columns=value_columns,
        segment_columns=segment_columns)


DETAIL_COLUMNS = ["txn_id", "txn_ts", "merchant_id", "status", "amount", "currency",
                  "failure_code", "latency_ms", "is_retry"]


def _compile_detail(plan: AnalysisPlan, model: SemanticModel, base: str, tcol: str,
                    cte: str, cte_name: str, params: dict[str, Param],
                    dims: list[Dimension], filter_dims: list[Dimension]) -> CompiledQuery:
    from backend.core import db
    available = db.table_columns().get(base, frozenset())
    cols = [c for c in DETAIL_COLUMNS if c in available] or sorted(available)[:8]
    select = [f"t.{c}" for c in cols]
    for d in dims:
        select.append(f"{_dim_expr(d, base)} AS {d.id}")

    where = [f"t.{tcol} >= :win_start AND t.{tcol} < :win_end"]
    for d in dims + filter_dims:
        if d.implies_filter and d.implies_filter not in where:
            where.append(d.implies_filter)
    for i, f in enumerate(plan.filters):
        d = model.dimension(f.dimension)
        pname = f"f{i}"
        if f.op in ("in", "not in"):
            params[pname] = Param(name=pname, type="list", value=list(f.values))
            where.append(f"{_dim_expr(d, base)} {f.op.upper()} :{pname}")
        else:
            params[pname] = Param(name=pname, type="string",
                                  value=f.values[0] if f.values else None)
            where.append(f"{_dim_expr(d, base)} {f.op} :{pname}")

    sql = ["WITH " + cte, "SELECT", "  " + ",\n  ".join(select), f"FROM {cte_name} t"]
    sql += ["  " + j for j in _joins_for(model, base, dims + filter_dims)]
    sql.append("WHERE " + "\n  AND ".join(where))
    sql.append(f"ORDER BY t.{tcol} DESC")
    return CompiledQuery(
        template_sql="\n".join(sql), params=params,
        tables=sorted({base} | {d.table for d in dims + filter_dims}),
        is_aggregate=False, value_columns=cols,
        segment_columns=[d.id for d in dims])

def compile_freeform(sql_text: str, model: SemanticModel) -> CompiledQuery:
    """Escape hatch for a question the planner could not express as a plan."""
    root = sqlglot.parse_one(sql_text, read="duckdb")
    tables = sorted({(t.name or "").lower() for t in root.find_all(exp.Table) if t.name})
    sel = root.this if isinstance(root, exp.With) else root
    is_agg = bool(list(sel.find_all(exp.AggFunc))) or bool(sel.args.get("group"))
    return CompiledQuery(template_sql=sql_text, params={}, tables=tables,
                         is_aggregate=is_agg, value_columns=[], segment_columns=[])


def to_count_query(bound_sql: str) -> str:
    """
    Wrap a bound query to count its rows, for "showing 5,000 of N".

    Strips ORDER BY / LIMIT, which are meaningless under a count and would
    otherwise cap the count at the sentinel.
    """
    root = sqlglot.parse_one(bound_sql, read="duckdb")
    target = root.this if isinstance(root, exp.With) else root
    for key in ("order", "limit", "offset"):
        if isinstance(target, exp.Select):
            target.set(key, None)
    return (f"SELECT count(*) AS total_rows FROM (\n"
            f"{root.sql(dialect='duckdb', pretty=True)}\n) _cnt")


def plan_param_names(cq: CompiledQuery) -> list[str]:
    return sorted(cq.params)
