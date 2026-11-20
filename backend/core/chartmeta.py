"""
Chart validation. Ported from Fractal AI's sql_rag._fill_chart_meta / _is_id_col.

The LLM proposes a chart alongside the SQL; this module checks the proposal against
the REAL result shape and overrides when it is impossible. Every override is recorded
and shown in the evidence panel, so the user can see the system corrected the model.

Fractal AI's prompt also asks for a `group_by` field which nothing ever reads — dead
output, dropped here.
"""
from __future__ import annotations

from backend.core.schemas import AnalysisPlan, ChartSpec

ID_SUFFIXES = ("_id", "_key", "_code", "_num", "_number")
TIME_HINTS = ("bucket", "date", "day", "week", "month", "ts", "period", "hour")
MAX_SERIES = 8


def is_id_column(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(ID_SUFFIXES) or n in ("txn_id", "merchant_id", "bank_id", "pm_id")


def _numeric_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return [k for k, v in rows[0].items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and not is_id_column(k) and not k.startswith("_")]


def _label_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return [k for k, v in rows[0].items()
            if (isinstance(v, str) or v is None) and not k.startswith("_")]


def _rule_chart(plan: AnalysisPlan, rows: list[dict]) -> ChartSpec:
    """Deterministic fallback, keyed on the plan's shape rather than the model's guess."""
    nums, labels = _numeric_columns(rows), _label_columns(rows)
    y = plan.metric if plan.metric in (nums or []) else (nums[0] if nums else None)

    if not y:
        return ChartSpec(chart_type="table", title="Results")
    if plan.needs_decomposition and plan.baseline:
        return ChartSpec(chart_type="waterfall", x="component", y=y,
                         title="What moved the metric")
    if plan.time_grain and "bucket" in (labels + nums):
        series = plan.segment_by[0] if plan.segment_by else None
        return ChartSpec(chart_type="line", x="bucket", y=y, series=series,
                         title=f"{plan.metric.replace('_', ' ').title()} over time")
    if not plan.segment_by and len(rows) <= 1:
        return ChartSpec(chart_type="kpi", y=y, title=plan.metric.replace("_", " ").title())
    if plan.segment_by:
        x = plan.segment_by[0]
        wide = len(rows) > MAX_SERIES
        return ChartSpec(chart_type="horizontal_bar" if wide else "bar", x=x, y=y,
                         title=f"{plan.metric.replace('_', ' ').title()} by "
                               f"{x.replace('_', ' ')}")
    return ChartSpec(chart_type="table", title="Results")


def validate_chart(proposed: ChartSpec | None, rows: list[dict], plan: AnalysisPlan
                   ) -> ChartSpec:
    """Keep the model's proposal where it is possible; override where it is not."""
    fallback = _rule_chart(plan, rows)
    if proposed is None:
        fallback.overrides.append("no chart proposed by the model; derived from plan shape")
        return fallback

    spec = proposed.model_copy(deep=True)
    overrides: list[str] = list(spec.overrides)
    cols = set(rows[0]) if rows else set()
    nums, labels = _numeric_columns(rows), _label_columns(rows)

    # A waterfall's axis is synthetic (baseline / mix / rate / current) and a KPI
    # has no axis at all, so neither x column exists in the result set. Validating
    # them against result columns produces a nonsense "used 'component'" override.
    SYNTHETIC_X = {"waterfall", "kpi"}

    if not rows:
        return ChartSpec(chart_type="table", title=spec.title or "No rows",
                         overrides=overrides + ["no rows returned; forced table"])

    if spec.chart_type == "waterfall":
        pass        # values come from the decomposition, not from a result column
    elif spec.y not in cols or spec.y not in nums:
        bad = spec.y
        spec.y = fallback.y
        overrides.append(f"y '{bad}' is not a numeric result column; used '{spec.y}'")
    if spec.y and is_id_column(spec.y):
        overrides.append(f"y '{spec.y}' is an identifier, not a measure; forced table")
        spec.chart_type = "table"
    if spec.chart_type not in SYNTHETIC_X and spec.x and spec.x not in cols:
        bad = spec.x
        spec.x = fallback.x
        overrides.append(f"x '{bad}' is not a result column; used '{spec.x}'")
    if spec.series and spec.series not in cols:
        bad = spec.series
        spec.series = None
        overrides.append(f"series '{bad}' is not a result column; dropped")

    if spec.chart_type == "line":
        xl = (spec.x or "").lower()
        if not any(h in xl for h in TIME_HINTS):
            overrides.append(f"line chart needs a time axis; '{spec.x}' is not temporal, "
                             "switched to bar")
            spec.chart_type = "bar"
    if spec.chart_type in ("bar", "stacked_bar") and len(rows) > MAX_SERIES * 2:
        overrides.append(f"{len(rows)} categories is too many for a vertical bar; "
                         "switched to horizontal_bar")
        spec.chart_type = "horizontal_bar"
    if spec.series and spec.series in cols:
        card = len({r.get(spec.series) for r in rows})
        if card > MAX_SERIES:
            overrides.append(f"series '{spec.series}' has {card} values (> {MAX_SERIES}); "
                             "chart shows the top 7 plus Other")
    if spec.chart_type == "pie" and len(rows) > 10:
        overrides.append("pie is unreadable beyond 10 slices; switched to horizontal_bar")
        spec.chart_type = "horizontal_bar"
    if not spec.y and spec.chart_type != "table":
        overrides.append("no usable measure; forced table")
        spec.chart_type = "table"
    if not spec.title:
        spec.title = fallback.title

    spec.overrides = overrides
    return spec
