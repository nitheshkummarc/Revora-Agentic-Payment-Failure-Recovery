"""Replay the synthetic dataset through the agent orchestrator.

Loads `data/synthetic_events_500.json`, drives the mock gateway through each
row's declared setup steps, then runs the full Observe -> Trace -> Plan ->
Validate -> Execute -> Verify loop over the whole batch and writes
`data/batch_results.json`.

Reproducibility. Webhook delivery failure is re-rolled on every delivery
attempt from an RNG seeded at gateway construction, so a gateway instance that
has already served a batch starts the next one at a different point in the
stream. `reset()` is therefore called explicitly before seeding rather than
relying on the instance being fresh: the guarantee should be stated in the code
that depends on it, not inherited by accident.

Clock. Both the gateway and the orchestrator run on one injectable clock
anchored to the dataset's `batch_reference_time`. It moves exactly twice: once
from the seed instant to the reference instant, and not at all during the batch
itself, so every payment is observed at the same moment. That is what makes a
dropped webhook read as silence and a delayed one read as still in flight.

Model. `_select_llm_client()` picks the first available option in this order:
Groq (with Gemini as an automatic fallback if both keys are present), Gemini
alone, then the offline stub -- each gated on its own API key being set. The
stub is a fallback for no key at all, not the default. A stubbed run
needs no key and returns the same recommendation for every event, so everything
it measures is a property of the deterministic pipeline -- resolution, tracing,
policy and verification -- and not of any model's judgement. Figures from a
stubbed run must be described that way; check the printed "Model:" line to know
which one ran.

Usage:
    python data/run_batch.py [--dataset PATH] [--failure-rate 0.05]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import GatewaySettings  # noqa: E402
from app.gateway.mock_gateway import MockPaymentGateway  # noqa: E402
from app.gateway.schemas import (  # noqa: E402
    ChaosConfig,
    CreatePaymentRequest,
    ErrorObject,
    FailPaymentRequest,
    SimulateWebhookRequest,
)
from app.intelligence.llm_client import (  # noqa: E402
    FallbackLLMClient,
    GeminiLLMClient,
    GroqLLMClient,
    IntelligenceLayer,
    LLMClient,
    StubLLMClient,
)
from app.orchestrator.orchestrator import (  # noqa: E402
    AgentOrchestrator,
    BatchEvent,
    BatchResults,
    EventOutcome,
)

DATASET_PATH = Path(__file__).resolve().parent / "synthetic_events_500.json"
RESULTS_PATH = Path(__file__).resolve().parent / "batch_results.json"

PAISE_PER_RUPEE = 100


class BatchClock:
    """A clock the caller moves explicitly. Nothing here sleeps."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def set(self, moment: datetime) -> datetime:
        self.t = moment
        return self.t


def _rupees(paise: int) -> str:
    return f"Rs.{paise / PAISE_PER_RUPEE:,.2f}"


def _chaos_config(raw: Dict[str, Any] | None) -> ChaosConfig | None:
    return ChaosConfig(**raw) if raw else None


def apply_step(gateway: MockPaymentGateway, step: Dict[str, Any]) -> None:
    """Execute one declared gateway operation.

    An unrecognised op raises rather than being skipped: a dataset the runner
    only partly understands would seed payments into states the rows do not
    describe, and every downstream figure would be quietly wrong.
    """
    op = step["op"]
    if op == "create_payment":
        gateway.create_payment(
            CreatePaymentRequest(
                payment_id=step["payment_id"],
                amount=step["amount"],
                currency=step["currency"],
                notes=step["notes"],
            )
        )
    elif op == "fail_payment":
        gateway.fail_payment(
            FailPaymentRequest(
                payment_id=step["payment_id"],
                error=ErrorObject(**step["error"]),
                chaos=_chaos_config(step["chaos"]),
            )
        )
    elif op == "simulate_webhook":
        gateway.simulate_webhook(
            SimulateWebhookRequest(
                entity_id=step["payment_id"],
                event=step["event"],
                chaos=_chaos_config(step["chaos"]),
            )
        )
    else:
        raise ValueError(f"unknown gateway seed op: {op!r}")


def _select_llm_client(force_stub: bool = False) -> LLMClient:
    """Groq (+ Gemini fallback if both keys present), then Gemini alone, then
    the offline stub -- each gated on its own API key.

    The stub is a fallback for no key at all, not the default -- a run with a
    key available must exercise a real model, not silently fall back to
    canned output. Groq is checked first because both providers give the same
    schema-validated structured-output guarantee, and Groq is the one that
    answers reliably: Gemini's free tier caps at a request quota a 500-row
    batch can exhaust partway through, so leading with it means a demo run
    degrading into fallback calls rather than starting on the path it stays
    on.

    `force_stub` overrides all of the above. Without it, adding a real API
    key to the environment silently changes what this script measures -- the
    documented deterministic baseline (headline batch numbers, reproducibility
    claims) was generated with the stub, and there was previously no way to
    reproduce that baseline once a real key was present, short of unsetting
    it.
    """
    if force_stub:
        print("Model: StubLLMClient (--stub passed; ignoring any API keys present)")
        return StubLLMClient()
    groq_key = os.environ.get("GROQ_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

    if groq_key and gemini_key:
        print("Model: GroqLLMClient with GeminiLLMClient fallback (GROQ_API_KEY and GEMINI_API_KEY present)")
        return FallbackLLMClient(primary=GroqLLMClient(), fallback=GeminiLLMClient())
    if groq_key:
        print("Model: GroqLLMClient (GROQ_API_KEY present; set GEMINI_API_KEY too for a fallback)")
        return GroqLLMClient()
    if gemini_key:
        print("Model: GeminiLLMClient (GEMINI_API_KEY present, no GROQ_API_KEY)")
        return GeminiLLMClient()
    print("Model: StubLLMClient (no model API key present; offline deterministic client)")
    return StubLLMClient()


def seed_and_run(
    dataset: Dict[str, Any], failure_rate: float, force_stub: bool = False
) -> BatchResults:
    reference = datetime.fromisoformat(dataset["batch_reference_time"])
    seed_at = reference - timedelta(seconds=dataset["seed_offset_seconds"])

    clock = BatchClock(seed_at)
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=failure_rate),
        clock=clock,
    )
    # Rewind the delivery RNG before anything consumes it, so this run's
    # figures are reproducible rather than dependent on the instance's history.
    gateway.reset()

    rows = dataset["events"]

    for row in rows:
        for step in row["gateway_seed"]["setup"]:
            apply_step(gateway, step)

    # Everything seeded above has now been silent, or delivered, for this long.
    clock.set(reference)

    # Seeded last and deliberately: a delayed webhook is only still in flight
    # if it was emitted within its delay window of the observation instant.
    for row in rows:
        for step in row["gateway_seed"]["at_observation"]:
            apply_step(gateway, step)

    orchestrator = AgentOrchestrator(
        gateway=gateway,
        intelligence=IntelligenceLayer(llm_client=_select_llm_client(force_stub), clock=clock),
        clock=clock,
        results_path=RESULTS_PATH,
    )
    results = orchestrator.run_batch([BatchEvent(**row["batch_event"]) for row in rows])
    _report_dropped_webhooks(gateway)
    return results


def _report_dropped_webhooks(gateway: MockPaymentGateway) -> None:
    """Delivery failures that exhausted the retry budget.

    Reported rather than assumed absent. A webhook that never landed changes
    that payment's evidence, so it can move a row from clean failure to silence
    and change its outcome. At the default rate this is normally zero, and a
    non-zero count is a caveat on the figures below, not a defect.
    """
    exhausted = [
        entry
        for entry in gateway.delivery_client.delivery_log
        if not entry.succeeded and entry.attempt == gateway.settings.delivery_max_attempts
    ]
    total = len(gateway.delivery_client.delivery_log)
    print(f"Webhook delivery: {total} attempts, {len(exhausted)} exhausted the retry budget")
    if gateway.delivery_client.circuit_open:
        print("  WARNING: the delivery circuit breaker is open; later webhooks never landed")


def report(results: BatchResults, dataset: Dict[str, Any]) -> None:
    events = results.events
    summary = results.summary

    print()
    print(f"Batch run {results.batch_run_id}")
    print(f"  dataset       {dataset['count']} events, seed {dataset['seed']}")
    print(f"  observed at   {dataset['batch_reference_time']}")
    print()

    print("Outcome counts:")
    for label, value in (
        ("recovered", summary.recovered),
        ("blocked", summary.blocked),
        ("escalated", summary.escalated),
        ("needs_review", summary.needs_review),
        ("no_action", summary.no_action),
    ):
        print(f"  {label:<14} {value:>4}  ({value / summary.total_events:>5.1%})")
    print(f"  {'total':<14} {summary.total_events:>4}")

    by_outcome: Dict[str, int] = {}
    for event in events:
        by_outcome[event.outcome.value] = by_outcome.get(event.outcome.value, 0) + event.amount

    total_paise = sum(e.amount for e in events)
    no_action_paise = by_outcome.get(EventOutcome.NO_ACTION.value, 0)
    # Payments that never failed need no recovery, so counting them in the
    # denominator would inflate the rate with money that was never at risk.
    addressable = total_paise - no_action_paise
    recovered_paise = by_outcome.get(EventOutcome.RECOVERED.value, 0)
    blocked_paise = by_outcome.get(EventOutcome.BLOCKED.value, 0)

    print()
    print("Money moved through verified-safe paths")
    print("(paise internally, shown in rupees; not a claim about retry success):")
    print(f"  batch value            {_rupees(total_paise):>18}")
    print(f"  never at risk          {_rupees(no_action_paise):>18}  (resolved without action)")
    print(f"  addressable at risk    {_rupees(addressable):>18}")
    print(f"  settled via retry      {_rupees(recovered_paise):>18}")
    print(f"  preserved by policy    {_rupees(blocked_paise):>18}")
    for label, outcome in (
        ("escalated", EventOutcome.ESCALATED),
        ("needs review", EventOutcome.NEEDS_REVIEW),
    ):
        print(f"  {label:<22} {_rupees(by_outcome.get(outcome.value, 0)):>18}")
    if addressable:
        # Deliberately not called a recovery rate. In the mock gateway a retry
        # always succeeds, so this measures whether each payment reached its
        # correct decision, not how often a real retry would land.
        print(
            f"  correctly routed rate  {recovered_paise / addressable:>17.1%}"
            "  (of addressable value)"
        )

    print()
    print("Outcome by bucket:")
    bucket_of = {row["batch_event"]["payment_id"]: row["bucket"] for row in dataset["events"]}
    matrix: Dict[str, Dict[str, int]] = {}
    for event in events:
        bucket = bucket_of[event.payment_id]
        matrix.setdefault(bucket, {})
        matrix[bucket][event.outcome.value] = matrix[bucket].get(event.outcome.value, 0) + 1
    for bucket in sorted(matrix):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(matrix[bucket].items()))
        print(f"  {bucket:<20} {parts}")

    blocked_rules: Dict[str, int] = {}
    for event in events:
        if event.outcome is EventOutcome.BLOCKED and event.rule_id:
            blocked_rules[event.rule_id] = blocked_rules.get(event.rule_id, 0) + 1
    print()
    print("Policy blocks by rule:")
    for rule in sorted(blocked_rules):
        print(f"  {rule:<50} {blocked_rules[rule]:>4}")

    flagged = sum(1 for e in events if e.injection_patterns_flagged)
    print()
    print(f"Rows carrying a flagged injection pattern: {flagged}")
    print(f"Rows on the human review list:             {len(results.needs_human_review)}")
    print()
    print(f"Wrote {RESULTS_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the synthetic dataset end to end.")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument(
        "--failure-rate",
        type=float,
        default=GatewaySettings().webhook_delivery_failure_rate,
        help="simulated webhook delivery failure rate",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help=(
            "force the offline StubLLMClient regardless of any API key present "
            "in the environment -- reproduces the documented deterministic "
            "baseline even when GROQ_API_KEY/GEMINI_API_KEY are set"
        ),
    )
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    results = seed_and_run(dataset, args.failure_rate, force_stub=args.stub)
    report(results, dataset)


if __name__ == "__main__":
    main()
