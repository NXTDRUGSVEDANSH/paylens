"""FastAPI app for PayLens."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import config
from backend.api.routes import ask as ask_routes
from backend.api.routes import meta as meta_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("paylens")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.core import db
    from backend.kb import seed, store
    from semantic.loader import get_model

    model = get_model()
    log.info("semantic model %s loaded (%d metrics, %d dimensions)",
             model.version, len(model.metrics), len(model.dimensions))
    try:
        db.connect()
        log.info("duckdb ready: %s", config.DUCKDB_PATH)
    except Exception as exc:  # noqa: BLE001
        log.error("duckdb unavailable: %s", exc)

    # Pay the one-off ONNX embedding model load at startup (~25s cold) so no
    # user request eats it, then seed the cache if the model version changed.
    if store.available():
        store.warm()
        try:
            result = seed.seed(model)
            log.info("knowledge base: %s", result)
        except Exception as exc:  # noqa: BLE001
            log.warning("KB seeding failed: %s", exc)
    else:
        log.warning("knowledge base unavailable — running without the semantic cache")

    if not config.LLM_API_KEY:
        log.warning("LLM_API_KEY is not set — planner and narrator will use the "
                    "deterministic path (the system remains fully functional)")
    yield


app = FastAPI(title="PayLens", version="0.1.0",
              description="Enterprise AI Business Analyst for payments data",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.include_router(ask_routes.router)
app.include_router(meta_routes.router)



@app.get("/health")
def health() -> dict:
    from backend.core import db
    from backend.kb import store
    from semantic.loader import get_model
    out: dict = {"status": "ok", "llm_configured": bool(config.LLM_API_KEY)}
    try:
        out["semantic_model"] = get_model().version
    except Exception as exc:  # noqa: BLE001
        out.update(status="degraded", semantic_model_error=str(exc))
    try:
        out["transactions"] = db.cursor().execute(
            "select count(*) from fact_transactions").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        out.update(status="degraded", db_error=str(exc))
    try:
        out["kb_records"] = store.count() if store.available() else 0
    except Exception:  # noqa: BLE001
        out["kb_records"] = 0
    return out


# Mounted LAST, deliberately. Starlette matches routes in registration order and a
# Mount at "/" matches every path, so registering it earlier would shadow /health
# and every API route behind it.
_FRONTEND = config.ROOT / "frontend"
if _FRONTEND.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
