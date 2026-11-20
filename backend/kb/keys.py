"""
Cache keys. `shape_key` is the hard equality gate that embedding similarity may
only RANK within — similarity never authorises reuse on its own.

Chroma metadata values must be scalars and `where` has no substring matching, so a
list of metric/dimension tags cannot be filtered on. That is why shape_key exists:
one scalar encoding the exact metric + dimension + intent combination.
"""
from __future__ import annotations

import hashlib


def shape_key(*, intent: str, metrics: list[str], dimensions: list[str],
              filter_dimensions: list[str], time_grain: str | None,
              has_baseline: bool, model_version: str) -> str:
    """
    Note: filter DIMENSION NAMES, never filter VALUES. Including values would make
    the record tenant- and value-specific, defeating cache sharing and leaking one
    user's filter into another's few-shot example.
    """
    parts = [
        intent,
        ",".join(sorted(metrics)),
        ",".join(sorted(dimensions)),
        ",".join(sorted(filter_dimensions)),
        time_grain or "-",
        "base" if has_baseline else "nobase",
        model_version,
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def record_id(shape: str, canonical_question: str) -> str:
    """
    Keyed on (shape, canonical question) so a re-answer UPSERTS.

    Fractal AI keys on md5(json({question, sql})), which means the same question with
    different SQL produces TWO live records, both retrievable, with no way to tell
    which is newer — so top-k can hand the model two contradictory exemplars for one
    question.
    """
    return hashlib.sha1(f"{shape}|{canonical_question}".encode()).hexdigest()[:24]
