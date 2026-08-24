"""Revora FastAPI application entrypoint.

Single process, no authentication, no database -- all state is in memory.
"""

from __future__ import annotations

import os

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

# The dashboard is a separate origin, so it needs an explicit allowance.
#
# Scoped to the origins the dashboard is actually served from rather than "*".
# Nothing here is credentialed and nothing is writable, so a wildcard would not
# have leaked anything -- but "*" on a public demo invites any page on the
# internet to read the endpoint, and the narrower list costs nothing.
#
# Override for a different host with a comma-separated REVORA_CORS_ORIGINS.
# Vite dev server, then `vite preview`, on both spellings of loopback: a browser
# treats localhost and 127.0.0.1 as distinct origins.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)


def _cors_origins() -> list[str]:
    configured = os.environ.get("REVORA_CORS_ORIGINS", "").strip()
    if not configured:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
