"""Top-level API router.

Thin aggregator: each module owns its own router and this file only mounts
them. Modules 2-8 append their routers here as they are built.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.gateway.mock_gateway import router as gateway_router

api_router = APIRouter()
api_router.include_router(gateway_router)
