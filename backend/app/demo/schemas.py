"""Request and response shapes for the agent demo API."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator.schemas import EventTrace


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoScenario(StrictModel):
    """One dataset row offered as a demo, described for a reader."""

    payment_id: str
    scenario: str
    bucket: str
    intent: str
    amount: int = Field(description="Paise")
    customer_note: Optional[str] = None
    expected_policy_rule: Optional[str] = None


class ScenarioList(StrictModel):
    live_available: bool
    scenarios: List[DemoScenario]


class AgentRunRequest(StrictModel):
    payment_id: str = Field(min_length=1, max_length=64)
    live: bool = False


class AgentRunResponse(StrictModel):
    scenario: DemoScenario
    # "live" only when the live agent was requested and is available.
    mode: Literal["scripted", "live"]
    trace: EventTrace
    # The committed single-call run's result for the same payment, so the two
    # can be compared. None if the results file is unavailable.
    single_call_outcome: Optional[str] = None
    single_call_final_action: Optional[str] = None
