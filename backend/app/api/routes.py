"""Top-level API router.

Thin aggregator: each package owns its own router and this file only mounts
them.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.gateway.mock_gateway import router as gateway_router

api_router = APIRouter()
api_router.include_router(gateway_router)
