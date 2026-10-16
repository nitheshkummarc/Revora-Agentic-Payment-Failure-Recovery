"""HTTP routes for the agent demo.

`GET /api/agent/scenarios` lists one runnable row per dataset scenario.
`POST /api/agent/run` runs the planning agent on one of them and returns the
full trace, including every tool call the agent made.

Limits are local design choices for a free-tier demo, not product figures.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Request

from app.demo.ratelimit import RateLimiter
from app.demo.schemas import AgentRunRequest, AgentRunResponse, ScenarioList
from app.demo.service import (
    UnknownScenarioError,
    list_scenarios,
    live_agent_available,
    run_scenario,
)

router = APIRouter(prefix="/api/agent", tags=["agent demo"])

#: Scripted runs cost nothing but CPU; live runs spend the provider's quota.
SCRIPTED_RUNS_PER_VISITOR = RateLimiter(limit=30, window_seconds=60)
LIVE_RUNS_PER_VISITOR = RateLimiter(limit=5, window_seconds=600)
LIVE_RUNS_GLOBAL = RateLimiter(limit=60, window_seconds=3600)


def _visitor(request: Request) -> str:
    # Behind a hosting proxy the client address is the proxy's; the first
    # X-Forwarded-For entry is the visitor. It can be spoofed, which only lets
    # a caller pick their own bucket -- the global live limit still applies.
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "unknown")


def _limit(limiter: RateLimiter, key: str) -> None:
    wait = limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many runs. Try again shortly.",
            headers={"Retry-After": str(math.ceil(wait))},
        )


@router.get("/scenarios", response_model=ScenarioList)
def scenarios() -> ScenarioList:
    return ScenarioList(live_available=live_agent_available(), scenarios=list_scenarios())


@router.post("/run", response_model=AgentRunResponse)
def run(body: AgentRunRequest, request: Request) -> AgentRunResponse:
    visitor = _visitor(request)
    live = body.live and live_agent_available()
    if live:
        _limit(LIVE_RUNS_PER_VISITOR, visitor)
        _limit(LIVE_RUNS_GLOBAL, "global")
    else:
        _limit(SCRIPTED_RUNS_PER_VISITOR, visitor)
    try:
        return run_scenario(body.payment_id, live=live)
    except UnknownScenarioError:
        raise HTTPException(status_code=404, detail=f"no scenario for payment {body.payment_id}")
