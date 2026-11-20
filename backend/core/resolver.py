"""
Deterministic resolution: question -> metrics, dimensions, window, intent.

Runs BEFORE any LLM call, and for most questions settles everything. The planner is
only needed when this pass is ambiguous. That makes the common case free, and it is
also what produces the cache key, so the cache lookup never costs a model call.

This module is "Metric Scout job A" from the engine plan: it serves the brief's
step 2, "the system identifies relevant datasets, measures and dimensions", and it
is where the ambiguity failure scenario is handled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backend import config
from backend.core.schemas import Assumption, Filter, TimeWindow
from backend.kb.keys import shape_key
from backend.kb.normalize import canonical_form, residual_literals
from semantic.loader import SemanticModel

MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october "
    "november december".split())}

_INTENT_RULES: list[tuple[str, str]] = [
    (r"\b(why|what (?:caused|drove)|explain|reason for|root cause|driver)\b", "explain_change"),
    (r"\b(trend|over time|by (?:day|week|month|hour)|daily|weekly|monthly|time series)\b", "trend"),
    (r"\b(top|bottom|best|worst|highest|lowest|rank|leading)\b", "rank"),
    (r"\b(compare|versus|vs\.?|against|cohort|side by side)\b", "compare"),
    (r"\b(list|individual|raw rows?|sample rows?|every (?:single )?\w+ row|"
     r"show me (?:every|all|the)?\s*(?:single\s+)?(?:rows?|transactions?|records?)|"
     r"dump|export)\b", "detail"),
    (r"\bby\b", "breakdown"),
]
_DROP_WORDS = re.compile(r"\b(dropped|fall|fell|decline|declined|down|worse|"
                         r"spike|rise|rose|increase|up|improve)\b", re.I)

# Destructive INTENT expressed in natural language. PayLens never executes
# user-supplied SQL, so injection is structurally impossible — but a user asking
# to mutate data deserves a clear "this tool is read-only", not an analytics
# answer about the word "transactions" that happens to be in their sentence.
# Two narrow patterns, deliberately NOT a bare verb list. "drop" is the trap:
# "why did our success rate drop last week" is the single most important question
# this product answers, and a naive \bdrop\b refuses it. So `drop` only counts
# when applied to a database object, and the bare verbs only count in the
# imperative position where a user is actually commanding a mutation.
_DDL_PHRASE = re.compile(
    r"\b(?:drop|truncate|alter|rename)\s+(?:the\s+)?(?:table|database|schema|column|"
    r"index|view)\b|\b(?:insert\s+into|grant\s+\w+\s+on|revoke\s+\w+\s+on)\b", re.I)
_IMPERATIVE_MUTATION = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|now\s+)*"
    r"(delete|remove|erase|wipe|purge|truncate|update|overwrite|modify)\b"
    # A preposition after the verb means it is a NOUN, not a command:
    # "update on payment performance" is a request for a briefing, not a mutation.
    r"(?!\s+(?:on|about|for|regarding|re|of|in)\b)", re.I)


@dataclass
class Resolution:
    question: str
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    window: TimeWindow | None = None
    baseline: TimeWindow | None = None
    intent: str = "single_value"
    time_grain: str | None = None
    top_n: int | None = None
    canonical_question: str = ""
    shape: str = ""
    param_names: list[str] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    residuals: list[str] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        """True when the planner can be skipped entirely."""
        return bool(self.metrics) and not self.ambiguities and not self.residuals


def now() -> datetime:
    return datetime.fromisoformat(config.DEMO_NOW)


def _complete_through(model: SemanticModel, today: date) -> date:
    lag = model.datasets["fact_transactions"].freshness_lag_days
    return today - timedelta(days=lag)


def resolve_window(text: str, model: SemanticModel, today: date | None = None
                   ) -> tuple[TimeWindow | None, TimeWindow | None, list[Assumption]]:
    """
    Returns (window, baseline, assumptions).

    Weeks are ISO (Mon..Sun) and only COMPLETE periods are used, because a
    part-ingested current period reads as a decline that is not real.
    """
    today = today or now().date()
    t = text.lower()
    assumptions: list[Assumption] = []
    this_mon = today - timedelta(days=today.weekday())

    def wk(offset: int) -> TimeWindow:
        start = this_mon - timedelta(days=7 * offset)
        return TimeWindow(start=start, end=start + timedelta(days=6))

    win = base = None

    if re.search(r"\blast week\b", t):
        win, base = wk(1), wk(2)
    elif re.search(r"\bthis week\b", t):
        win, base = TimeWindow(start=this_mon, end=today), wk(1)
        assumptions.append(Assumption(
            kind="time_window",
            statement=f"'this week' is incomplete; using {this_mon.isoformat()}..{today.isoformat()}",
            alternative="ask for 'last week' for a complete seven-day comparison"))
    elif m := re.search(r"\blast (\d+) days?\b", t):
        n = int(m.group(1))
        end = _complete_through(model, today)
        win = TimeWindow(start=end - timedelta(days=n - 1), end=end)
        base = TimeWindow(start=win.start - timedelta(days=n), end=win.start - timedelta(days=1))
    elif re.search(r"\blast month\b", t):
        first_this = today.replace(day=1)
        last_end = first_this - timedelta(days=1)
        win = TimeWindow(start=last_end.replace(day=1), end=last_end)
        prev_end = win.start - timedelta(days=1)
        base = TimeWindow(start=prev_end.replace(day=1), end=prev_end)
    elif re.search(r"\b(ever|all time|of all time|entire history|since launch)\b", t):
        win = TimeWindow(start=date(2000, 1, 1), end=today)
        assumptions.append(Assumption(
            kind="time_window",
            statement="Interpreted as all available history.",
            alternative="name a period to narrow it"))
    elif re.search(r"\byesterday\b", t):
        win = TimeWindow(start=today - timedelta(days=1), end=today - timedelta(days=1))
        base = TimeWindow(start=today - timedelta(days=2), end=today - timedelta(days=2))
    elif m := re.search(r"\b(" + "|".join(MONTHS) + r")\b", t):
        mon = MONTHS[m.group(1)]
        year = today.year if mon <= today.month else today.year - 1
        start = date(year, mon, 1)
        end = (date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1))
        win = TimeWindow(start=start, end=min(end, today))
        pm = mon - 1 or 12
        py = year if mon > 1 else year - 1
        pend = date(year, mon, 1) - timedelta(days=1)
        base = TimeWindow(start=date(py, pm, 1), end=pend)
    elif m := re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:to|-|\.\.)\s*(\d{4}-\d{2}-\d{2})", t):
        win = TimeWindow(start=date.fromisoformat(m.group(1)),
                         end=date.fromisoformat(m.group(2)))
        span = win.days
        base = TimeWindow(start=win.start - timedelta(days=span),
                          end=win.start - timedelta(days=1))

    if win is None:
        # No window stated. Assume the last complete week and SAY SO — blocking a
        # business user for a clarification they did not need is a worse product
        # than answering under a stated assumption.
        win, base = wk(1), wk(2)
        assumptions.append(Assumption(
            kind="time_window",
            statement=f"No time period was given, so this uses last week "
                      f"({win.label()}), the most recent complete week.",
            alternative="name a period, e.g. 'last 30 days' or 'October'"))
    return win, base, assumptions


def _detect_intent(text: str, dimensions: list[str], has_baseline_words: bool) -> str:
    t = text.lower()
    for pattern, intent in _INTENT_RULES:
        if re.search(pattern, t):
            if intent == "explain_change" or (has_baseline_words and _DROP_WORDS.search(t)):
                return "explain_change"
            return intent
    return "breakdown" if dimensions else "single_value"


_GRAIN_WORDS = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month"}


def _detect_grain(text: str) -> str | None:
    t = text.lower()
    for word, grain in _GRAIN_WORDS.items():      # "daily", not "dayly"
        if re.search(rf"\b{word}\b", t):
            return grain
    for g in ("hour", "day", "week", "month"):
        if re.search(rf"\b(?:by|per|each) {g}s?\b", t):
            return g
    return None


def _structural_words(model: SemanticModel) -> set[str]:
    """
    Words that name part of the MODEL rather than a value in the data.

    Without this, "how many transactions did merchant M-1042 process" matches the
    value 'merchant' in dim_failure_code.owner and silently adds
    `failure_owner IN ('merchant')` — a spurious filter that quietly narrows the
    answer. Same for "issuer bank" matching owner='issuer'.
    """
    words: set[str] = set()
    for d in model.dimensions.values():
        words |= {d.id.replace("_", " "), d.label.lower(), *(s.lower() for s in d.synonyms)}
    for m in model.metrics.values():
        words |= {m.id.replace("_", " "), m.label.lower(), *(s.lower() for s in m.synonyms)}
    return {w.strip() for w in words if w.strip()}


def _detect_filters(text: str, model: SemanticModel) -> list[Filter]:
    """Only values that actually exist in a dimension become filters."""
    from backend.core import db
    out: list[Filter] = []
    t = text.lower()
    blocked = _structural_words(model)
    for dim_id in ("payment_method", "merchant_tier", "region", "state", "failure_owner"):
        if dim_id not in model.dimensions:
            continue
        d = model.dimension(dim_id)
        try:
            vals = [r[d.column] for r in db.fetch_dicts(
                f"select distinct {d.column} from {d.table} where {d.column} is not null")]
        except Exception:  # noqa: BLE001
            continue
        hits = [v for v in vals
                if v and str(v).lower() not in blocked
                and re.search(rf"(?<![a-z0-9]){re.escape(str(v).lower())}(?![a-z0-9])", t)]
        if hits and len(hits) < len(vals):
            out.append(Filter(dimension=dim_id, op="in", values=hits))
    return out


def destructive_intent(question: str) -> str | None:
    """The verb being refused, or None. See the patterns above for why it is narrow."""
    if m := _DDL_PHRASE.search(question):
        return re.sub(r"\s+", " ", m.group(0).lower())
    if m := _IMPERATIVE_MUTATION.match(question):
        return m.group(1).lower()
    return None


_MERCHANT_ID_RE = re.compile(r"\bM-\d{3,5}\b", re.I)


def resolve(question: str, model: SemanticModel, today: date | None = None) -> Resolution:
    metrics = model.match_metrics(question)
    dimensions = model.match_dimensions(question)
    window, baseline, assumptions = resolve_window(question, model, today)
    grain = _detect_grain(question)
    has_base_words = bool(re.search(
        r"\b(vs|versus|compared|against|before|week[- ]on[- ]week|wow|mom)\b",
        question, re.I))
    intent = _detect_intent(question, dimensions, has_base_words)

    ambiguities: list[str] = []
    if not metrics:
        ambiguities.append(
            "No metric was named. Candidates: "
            + ", ".join(f"{m.id} ({m.label})" for m in list(model.metrics.values())[:4]))
    elif len(metrics) > 1 and intent != "compare":
        ambiguities.append(
            f"Multiple metrics matched ({', '.join(metrics)}); using '{metrics[0]}'.")
        assumptions.append(Assumption(
            kind="metric",
            statement=f"Several metrics matched; answered with '{metrics[0]}'.",
            alternative=f"ask specifically for {metrics[1]}"))

    # The denominator ambiguity the brief calls out: "success rate" has two defensible
    # definitions and the user cannot see which one they got unless we say.
    if metrics and metrics[0] == "success_rate" and not re.search(
            r"\b(pending|including pending|excluding pending)\b", question, re.I):
        m = model.metric("success_rate")
        assumptions.append(Assumption(
            kind="denominator",
            statement=("Success rate excludes PENDING attempts from the denominator "
                       "(denominator = SUCCESS + FAILED)."),
            alternative="use 'success_rate_incl_pending' to count PENDING as failures"))

    top_n = None
    if mt := re.search(r"\btop\s+(\d+)\b", question, re.I):
        top_n = int(mt.group(1))
    elif intent == "rank":
        top_n = 10

    # A trend already shows its own history; a second window just adds noise.
    if intent not in ("explain_change", "compare"):
        baseline = None

    terms: list[str] = []
    for mid in metrics:
        x = model.metric(mid)
        terms += [x.id.replace("_", " "), x.label.lower(), *x.synonyms]
    for did in dimensions:
        d = model.dimension(did)
        terms += [d.id.replace("_", " "), d.label.lower(), *d.synonyms]

    canonical, _kinds = canonical_form(
        question, metrics, dimensions, has_baseline=baseline is not None,
        synonym_terms=terms)
    residuals = residual_literals(canonical)

    filters = _detect_filters(question, model)

    # A named merchant is a FILTER, not a segmentation. Without this, "how many
    # transactions did merchant M-1042 process" returns every merchant and reports
    # the first row — a confidently wrong answer.
    named = sorted({m.upper() for m in _MERCHANT_ID_RE.findall(question)})
    if named and "merchant_id" in model.dimensions:
        filters.append(Filter(dimension="merchant_id", op="in", values=named))
        dimensions = [d for d in dimensions if d not in ("merchant", "merchant_id")]
        if intent == "breakdown" and not dimensions:
            intent = "single_value"
    params = ["win_start", "win_end"] + (["base_start", "base_end"] if baseline else [])
    params += [f"f{i}" for i in range(len(filters))]

    return Resolution(
        question=question, metrics=metrics, dimensions=dimensions, filters=filters,
        window=window, baseline=baseline, intent=intent, time_grain=grain, top_n=top_n,
        canonical_question=canonical,
        shape=shape_key(intent=intent, metrics=metrics, dimensions=dimensions,
                        filter_dimensions=[f.dimension for f in filters],
                        time_grain=grain, has_baseline=baseline is not None,
                        model_version=model.version),
        param_names=sorted(params), assumptions=assumptions, ambiguities=ambiguities,
        residuals=residuals)
