"""Revora FastAPI application entrypoint.

Single process, no authentication, no database -- all state is in memory.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import api_router
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(
    title="Revora",
    description=(
        "Payment-failure root-cause analysis and recovery decisioning "
        "with a deterministic diagnosis layer."
    ),
    version="0.1.0",
)

app.include_router(api_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
