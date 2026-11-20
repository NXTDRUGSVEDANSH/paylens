"""The /ask endpoint and friends."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core import cache
from backend.core.pipeline import ask as run_ask
from backend.core.schemas import AnswerCard
from semantic.loader import get_model

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    role: str = "cxo"
    claims: dict[str, str] = Field(default_factory=dict)
    today: date | None = None
    use_cache: bool = True


@router.post("/ask", response_model=AnswerCard)
def ask(req: AskRequest) -> AnswerCard:
    model = get_model()
    if req.role not in model.roles:
        raise HTTPException(400, f"unknown role '{req.role}'. "
                                 f"Known: {sorted(model.roles)}")
    try:
        return run_ask(req.question, role=req.role, claims=req.claims,
                       today=req.today, model=model, use_cache=req.use_cache)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@router.get("/cache/stats")
def cache_stats() -> dict:
    from backend.kb import store
    out = {"answer_cache": cache.stats()}
    try:
        out["kb"] = {"records": store.count(),
                     "verified": store.count(verified_only=True)}
    except Exception as exc:  # noqa: BLE001
        out["kb"] = {"error": str(exc)}
    return out


@router.post("/cache/clear")
def cache_clear() -> dict:
    return {"cleared": cache.invalidate()}


@router.post("/kb/reset")
def kb_reset(keep_seeds: bool = True) -> dict:
    """Restore the seed-only state so a demo run is reproducible."""
    from backend.kb import store
    try:
        n = store.reset(keep_seeds=keep_seeds)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc)) from exc
    cache.invalidate()
    return {"deleted": n, "kept_seeds": keep_seeds}
