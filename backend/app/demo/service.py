"""Runs the planning agent on one scenario from the dataset.

Each run builds its own gateway and seeds only the chosen row, so runs are
independent of each other and of the server's shared gateway. Webhook delivery
failure is turned off here (rate 0.0) so a scenario always plays out as the
dataset describes it; the chaos modes that define the scenarios are
deterministic and still apply.

Two agents are available:

* the scripted agent (`ScriptedAgentClient`), always available, deterministic
  and free;
* the live agent (`GroqAgentClient`), only when `REVORA_AGENT_LIVE` is set and a
  Groq key is configured.

Both go through the same `IntelligenceLayer`, so the guards apply to both.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import GatewaySettings, env_flag
from app.demo.schemas import AgentRunResponse, DemoScenario
from app.demo.seeding import BatchClock, apply_step
from app.gateway.mock_gateway import MockPaymentGateway
from app.intelligence.agent import GroqAgentClient, ScriptedAgentClient
from app.intelligence.llm_client import IntelligenceLayer
from app.orchestrator.orchestrator import REPO_ROOT, AgentOrchestrator
from app.orchestrator.schemas import BatchEvent

DATASET_PATH = REPO_ROOT / "data" / "synthetic_events_500.json"
BATCH_RESULTS_PATH = REPO_ROOT / "data" / "batch_results.json"


class UnknownScenarioError(LookupError):
    """No dataset row has the requested payment id."""


@lru_cache(maxsize=1)
def _dataset() -> Dict[str, Any]:
    return json.loads(Path(DATASET_PATH).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _batch_outcomes() -> Dict[str, Dict[str, Any]]:
    """The committed single-call run's outcome per payment, for comparison.
    Empty if the results file is missing or unreadable."""
    try:
        payload = json.loads(Path(BATCH_RESULTS_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {event["payment_id"]: event for event in payload.get("events", [])}


def _scenario(row: Dict[str, Any]) -> DemoScenario:
    event = row["batch_event"]
    return DemoScenario(
        payment_id=event["payment_id"],
        scenario=row["scenario"],
        bucket=row["bucket"],
        intent=row["intent"],
        amount=event["amount"],
        customer_note=event.get("customer_note"),
        expected_policy_rule=row.get("expected_policy_rule"),
    )


def list_scenarios() -> List[DemoScenario]:
    """One representative row per scenario, in dataset order."""
    seen: set = set()
    scenarios: List[DemoScenario] = []
    for row in _dataset()["events"]:
        if row["scenario"] in seen:
            continue
        seen.add(row["scenario"])
        scenarios.append(_scenario(row))
    return scenarios


def live_agent_available() -> bool:
    return env_flag("REVORA_AGENT_LIVE") and bool(os.environ.get("GROQ_API_KEY"))


_scripted_layer = IntelligenceLayer(llm_client=ScriptedAgentClient())
_live_layer: Optional[IntelligenceLayer] = None


def _layer(live: bool) -> IntelligenceLayer:
    """Shared layers, so the live layer's circuit breaker spans requests."""
    global _live_layer
    if not live:
        return _scripted_layer
    if _live_layer is None:
        _live_layer = IntelligenceLayer(llm_client=GroqAgentClient())
    return _live_layer


def run_scenario(payment_id: str, live: bool = False) -> AgentRunResponse:
    """Seed one row on a fresh gateway and run it through the pipeline in
    agent mode. `live` is honoured only when the live agent is available."""
    dataset = _dataset()
    row = next(
        (r for r in dataset["events"] if r["batch_event"]["payment_id"] == payment_id),
        None,
    )
    if row is None:
        raise UnknownScenarioError(payment_id)

    use_live = live and live_agent_available()
    reference = datetime.fromisoformat(dataset["batch_reference_time"])
    clock = BatchClock(reference - timedelta(seconds=dataset["seed_offset_seconds"]))
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=0.0), clock=clock
    )
    gateway.reset()
    for step in row["gateway_seed"]["setup"]:
        apply_step(gateway, step)
    clock.set(reference)
    for step in row["gateway_seed"]["at_observation"]:
        apply_step(gateway, step)

    orchestrator = AgentOrchestrator(
        gateway=gateway,
        intelligence=_layer(use_live),
        clock=clock,
        results_path=None,
        agent_mode=True,
    )
    trace = orchestrator.process_event(
        BatchEvent(**row["batch_event"]), batch_run_id=f"agent-{uuid.uuid4()}"
    )
    committed = _batch_outcomes().get(payment_id, {})
    return AgentRunResponse(
        scenario=_scenario(row),
        mode="live" if use_live else "scripted",
        trace=trace,
        single_call_outcome=committed.get("outcome"),
        single_call_final_action=committed.get("final_action"),
    )
