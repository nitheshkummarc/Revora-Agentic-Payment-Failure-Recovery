"""Top-level API router.

Thin aggregator: each package owns its own router and this file only mounts
them.

`REVORA_HIDE_GATEWAY_ROUTES` leaves the mock gateway's routes out. They are
unauthenticated and write to in-memory state, which is fine on a developer's
machine and not on a public server; the agent demo builds its own gateway per
run and does not need them.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import env_flag
from app.demo.routes import router as agent_demo_router
from app.gateway.mock_gateway import router as gateway_router
from app.orchestrator.orchestrator import router as orchestrator_router

api_router = APIRouter()
if not env_flag("REVORA_HIDE_GATEWAY_ROUTES"):
    api_router.include_router(gateway_router)
api_router.include_router(orchestrator_router)
api_router.include_router(agent_demo_router)
