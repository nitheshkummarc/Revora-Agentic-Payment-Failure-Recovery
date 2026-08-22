"""Revora FastAPI application entrypoint.

Single process, no authentication, no database -- all state is in memory.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

# The dashboard is a separate origin during development. Everything served
# here is a read-only view of a synthetic batch run -- there are no credentials
# to protect and no state a browser could change -- so the allowance is broad
# rather than carefully scoped. Narrow it before this is ever pointed at real
# data.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
