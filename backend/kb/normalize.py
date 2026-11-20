"""
Question canonicalisation — the safeguard that makes the >=0.95 cache tier safe.

MiniLM puts "success rate last week" and "success rate last month" at ~0.97-0.99
cosine. At a bare 0.95 gate, tier 1 would confidently answer the WRONG TIME WINDOW,
and because tier 1 skips the planner, nothing downstream would notice.

The fix is one idea applied in four places: move every semantically load-bearing
token out of the embedded text and into a hard-matched key, then re-bind it.

    "success rate last week vs the week before, by issuer"
      -> "<METRIC:success_rate> <WINDOW> vs <BASELINE> by <DIM:issuer_bank>"
      +  params {win_start, win_end, base_start, base_end}

The two questions now embed IDENTICALLY, which is correct: what differs has moved
into params, where it is compared exactly and then substituted. Collisions become
safe rather than rare.

Raising the threshold to 0.98 instead does NOT work — the collision pair sits above
it, and you lose the legitimate hits. Cosine over raw text is the wrong measurement,
not a too-loose one.
"""
from __future__ import annotations

import re

MONTHS = ("january february march april may june july august september october "
          "november december").split()

# Longest-first so "last week" wins over "week", and "previous month" over "month".
_WINDOW_PHRASES = [
    r"last \d+ (?:hours?|days?|weeks?|months?|quarters?)",
    r"past \d+ (?:hours?|days?|weeks?|months?|quarters?)",
    r"previous (?:week|month|quarter|year|period)",
    r"(?:the )?week before(?: last)?", r"(?:the )?month before(?: last)?",
    r"(?:the )?(?:prior|preceding) (?:week|month|quarter|period)",
    r"last (?:week|month|quarter|year)", r"this (?:week|month|quarter|year)",
    r"year to date", r"ytd", r"month to date", r"mtd",
    r"yesterday", r"today", r"so far this (?:week|month|quarter)",
    r"q[1-4](?:\s*(?:fy)?\s*\d{2,4})?",
    r"(?:" + "|".join(MONTHS) + r")(?:\s+\d{4})?",
    r"\d{4}-\d{2}-\d{2}(?:\s*(?:to|-|\.\.)\s*\d{4}-\d{2}-\d{2})?",
]
_WINDOW_RE = re.compile("|".join(f"(?:{p})" for p in
                                 sorted(_WINDOW_PHRASES, key=len, reverse=True)), re.I)
_BASELINE_HINT = re.compile(
    r"\b(?:vs|versus|compared (?:to|with)|against|relative to|week[- ]on[- ]week|"
    r"month[- ]on[- ]month|wow|mom|period[- ]over[- ]period)\b", re.I)
_MERCHANT_ID = re.compile(r"\bM-\d{3,5}\b", re.I)
_TOPN_RE = re.compile(r"\b(?:top|bottom|first|last)\s+\d+\b", re.I)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_CAPITALISED_PHRASE = re.compile(r"\b(?:[A-Z][a-z]{2,}\s+){1,}[A-Z][a-z]{2,}\b")
_QUOTED = re.compile(r"[\"'][^\"']{2,}[\"']")


def canonical_form(question: str, metric_ids: list[str], dimension_ids: list[str],
                   *, has_baseline: bool, synonym_terms: list[str] | None = None
                   ) -> tuple[str, list[str]]:
    """
    Returns (canonical_question, extracted_placeholder_kinds).

    Three transformations, in this order:

    1. Strip the metric/dimension wording out of the free text, because the resolved
       ids are appended as <METRIC:x>/<DIM:y> tokens. This is what makes "approval
       rate by rail" and "success rate by payment method" canonicalise IDENTICALLY —
       they are the same question and should share one cache entry.
    2. Replace time phrases positionally: the first becomes <WINDOW>, and when a
       comparison is implied the second becomes <BASELINE>. ("last week vs the week
       before" must not collapse into two indistinguishable <WINDOW> tokens.)
    3. Append the resolved ids, sorted, so word order cannot fork the key.
    """
    text = " " + question.strip().lower() + " "
    kinds: list[str] = []

    for term in sorted(synonym_terms or [], key=len, reverse=True):
        t = term.strip().lower()
        if len(t) >= 3:
            text = re.sub(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", " ", text)

    wants_baseline = bool(has_baseline or _BASELINE_HINT.search(question))
    text = _BASELINE_HINT.sub(" vs ", text)

    slots = ["<WINDOW>", "<BASELINE>"] if wants_baseline else ["<WINDOW>"]
    seen = {"n": 0}

    def _slot(_m: re.Match) -> str:
        i = seen["n"]
        seen["n"] += 1
        return f" {slots[i]} " if i < len(slots) else " <WINDOW> "

    text = _WINDOW_RE.sub(_slot, text)
    if seen["n"]:
        kinds.append("window")
    if wants_baseline:
        kinds.append("baseline")
        if "<BASELINE>" not in text:
            text += " vs <BASELINE>"

    text = _MERCHANT_ID.sub(" <ENTITY:merchant> ", text)
    # "top 5" / "top 20" differ only by a parameter, so the N is extracted too —
    # otherwise the digit survives as a residual literal and needlessly demotes
    # an otherwise perfectly cacheable question out of tier 1.
    text = _TOPN_RE.sub(" <TOPN> ", text)
    text = re.sub(r"\bby\b", " ", text)
    text = re.sub(r"[?.,!]", " ", text)

    for mid in sorted(metric_ids):
        text += f" <METRIC:{mid}>"
    for did in sorted(dimension_ids):
        text += f" <DIM:{did}>"

    return re.sub(r"\s+", " ", text).strip(), kinds


def residual_literals(canonical: str) -> list[str]:
    """
    Anything load-bearing that canonicalisation did NOT extract.

    A leftover digit, quoted string or capitalised multi-word phrase is precisely the
    signal that resolution did not understand the question — which is exactly when
    the planner must not be skipped. Cheap, and it fails in the safe direction.
    """
    stripped = re.sub(r"<[A-Z]+(?::[a-z_]+)?>", " ", canonical)
    out: list[str] = []
    out += _NUMBER.findall(stripped)
    out += [m.strip("\"'") for m in _QUOTED.findall(stripped)]
    out += _CAPITALISED_PHRASE.findall(stripped)
    return sorted(set(out))


def _trigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", " ", s.strip().lower())
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))} or {s}


def lexical_similarity(a: str, b: str) -> float:
    """
    Character-trigram Jaccard. Catches the residual case where canonicalisation
    missed a modifier ("success rate EXCLUDING RETRIES") that MiniLM also
    under-weights. Microseconds, ~10 lines, no model.
    """
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
