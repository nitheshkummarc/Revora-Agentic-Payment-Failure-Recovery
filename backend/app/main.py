"""RecoverX FastAPI application entrypoint.

Single process, no auth, no database -- per the global "do NOT build" list in
RecoverX_Build_Prompts.md.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import api_router
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(
    title="RecoverX",
    description=(
        "AI revenue recovery with a deterministic diagnosis layer. "
        "Schema and rules follow GROUND_TRUTH.md."
    ),
    version="0.1.0",
)

app.include_router(api_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
