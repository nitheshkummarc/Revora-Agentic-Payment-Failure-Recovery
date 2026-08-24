"""Compare Revora against a naive always-retry policy on identical incidents.

This is a controlled synthetic benchmark, not a measurement of production
behaviour. Both policies replay the same 500 generated rows, from the same seed
and the same reference instant, each against its own freshly reset gateway. The
only variable is the policy.

Why a fresh gateway per policy. Webhook delivery failure is re-rolled from an
RNG seeded at gateway construction, so a gateway that has already served one
policy starts the next at a different point in the stream. Running the two
policies against one instance would let the first silently change the second's
chaos outcomes, and the comparison would no longer be like-for-like.

Classification is state-first. Every payment's state is established BEFORE any
action is evaluated, and a payment is classified by that prior state rather than
by whether a policy's call happened to return success. A naive retry that
"succeeds" against a payment the gateway had already captured is not a recovery;
it is duplicate-payment risk, and it is counted as such.

The naive policy has no tracer, no policy engine and no verification stage. It
recommends RETRY_SOFT for every payment and acts on it.

Usage:
    python data/run_baseline.py [--dataset PATH] [--failure-rate 0.05]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "data"))

from app.core.config import GatewaySettings  # noqa: E402
from app.gateway.mock_gateway import MockPaymentGateway  # noqa: E402
from app.gateway.schemas import (  # noqa: E402
    CapturePaymentRequest,
    PaymentState,
    SimulateWebhookRequest,
    WebhookEventName,
)
from app.intelligence.llm_client import IntelligenceLayer, StubLLMClient  # noqa: E402
from app.intelligence.schemas import RecommendedAction  # noqa: E402
from app.orchestrator.orchestrator import (  # noqa: E402
    AgentOrchestrator,
    BatchEvent,
    EventOutcome,
)
from app.state_machine.resolver import StateResolver  # noqa: E402
from app.state_machine.schemas import PaymentObservation  # noqa: E402
from app.state_machine.states import CanonicalState  # noqa: E402

import run_batch  # noqa: E402

DATASET_PATH = Path(__file__).resolve().parent / "synthetic_events_500.json"
RESULTS_PATH = Path(__file__).resolve().parent / "baseline_comparison.json"

PAISE_PER_RUPEE = 100

#: States that mean the payment had already succeeded before anything acted on
#: it. Read from gateway truth, not from delivered evidence: a payment whose
#: success webhooks were dropped is still a success, and retrying it is a
#: duplicate-payment risk rather than a recovery.
ALREADY_SUCCESSFUL_STATES = frozenset({PaymentState.AUTHORIZED, PaymentState.CAPTURED})

#: Canonical states from which a recovery action is legitimate. PENDING_WEBHOOK
#: qualifies only because the resolver itself defines it as an unresolved
#: outcome requiring a status query -- it is not a new interpretation invented
#: here, and it is reached only after the already-successful test above.
RECOVERY_ELIGIBLE_STATES = frozenset({CanonicalState.FAILED, CanonicalState.PENDING_WEBHOOK})

CATEGORIES = (
    "legitimately_recovered_paise",
    "already_successful_preserved_paise",
    "incorrectly_put_at_risk_paise",
    "safely_blocked_paise",
    "escalated_paise",
)


def _rupees(paise: int) -> str:
    return f"Rs.{paise / PAISE_PER_RUPEE:,.2f}"


@dataclass
class PriorState:
    """What was true about a payment before any policy acted on it."""

    payment_id: str
    amount: int
    truth: PaymentState
    resolved: CanonicalState
    already_successful: bool
    recovery_eligible: bool


@dataclass
class PolicyRun:
    """One policy's outcome per payment, plus its own operational counters."""

    name: str
    #: payment_id -> the category it falls into.
    category: Dict[str, str] = field(default_factory=dict)
    #: Every assignment made, in order, including any that overwrote an earlier
    #: one. `category` alone cannot show a double assignment, because the second
    #: would silently replace the first.
    assignments: List[str] = field(default_factory=list)
    #: Payments this policy attempted a money-moving action against.
    retried: Set[str] = field(default_factory=set)
    #: Payments whose post-action state matched the expected recovery state.
    landed: Set[str] = field(default_factory=set)
    #: Payments routed to a human or to a status query instead of acted on.
    escalated: Set[str] = field(default_factory=set)
    #: Payments whose action the policy refused outright.
    blocked: Set[str] = field(default_factory=set)
    #: Mismatches the policy's own verification stage caught.
    verification_failures: int = 0
    unclassified: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------
def seed_gateway(dataset: Dict[str, Any], failure_rate: float):
    """Build a fresh gateway, reset it, and drive it through every row's setup.

    Returns the gateway and its clock, positioned at the observation instant.
    """
    reference = datetime.fromisoformat(dataset["batch_reference_time"])
    seed_at = reference - timedelta(seconds=dataset["seed_offset_seconds"])

    clock = run_batch.BatchClock(seed_at)
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=failure_rate),
        clock=clock,
    )
    # Rewind the delivery RNG before anything consumes it. Stated here because
    # the comparison depends on it: without this the second policy would run
    # against a different chaos stream than the first.
    gateway.reset()

    rows = dataset["events"]
    for row in rows:
        for step in row["gateway_seed"]["setup"]:
            run_batch.apply_step(gateway, step)

    clock.set(reference)

    for row in rows:
        for step in row["gateway_seed"]["at_observation"]:
            run_batch.apply_step(gateway, step)

    return gateway, clock


def capture_prior_states(gateway, clock, rows) -> Dict[str, PriorState]:
    """Establish every payment's state before any action is evaluated.

    Both readings are taken and kept. Gateway truth decides whether a payment
    had already succeeded; the resolver decides whether an unsuccessful one is
    recovery-eligible. Where the two disagree, that divergence is itself
    reported rather than smoothed over.
    """
    resolver = StateResolver()
    prior: Dict[str, PriorState] = {}

    for row in rows:
        payment_id = row["batch_event"]["payment_id"]
        snapshot = gateway.get_payment_status(payment_id)
        truth = snapshot.payment.state
        resolution = resolver.resolve(
            PaymentObservation(
                payment_id=payment_id,
                created_at=snapshot.payment.created_at,
                events=list(snapshot.event_history),
                observed_at=clock(),
            )
        )
        already = truth in ALREADY_SUCCESSFUL_STATES
        prior[payment_id] = PriorState(
            payment_id=payment_id,
            amount=row["batch_event"]["amount"],
            truth=truth,
            resolved=resolution.state,
            already_successful=already,
            # Evaluated only after the already-successful test, so a dropped
            # success webhook can never make a captured payment look failed.
            recovery_eligible=(not already) and resolution.state in RECOVERY_ELIGIBLE_STATES,
        )
    return prior


# --------------------------------------------------------------------------
# The naive policy
# --------------------------------------------------------------------------
def run_naive(gateway, rows) -> PolicyRun:
    """Always retry. No tracer, no policy engine, no verification.

    The post-action state is read here for classification only. That reading is
    the benchmark measuring what happened; it is not a verification stage the
    naive policy performs, and nothing in this function acts on it.
    """
    run = PolicyRun(name="naive always-retry")

    for row in rows:
        payment_id = row["batch_event"]["payment_id"]
        run.retried.add(payment_id)
        try:
            current = gateway.get_payment_status(payment_id).payment.state
            if current is not PaymentState.AUTHORIZED:
                gateway.simulate_webhook(
                    SimulateWebhookRequest(
                        entity_id=payment_id,
                        event=WebhookEventName.PAYMENT_AUTHORIZED,
                    )
                )
            gateway.capture_payment(CapturePaymentRequest(payment_id=payment_id))
        except Exception:
            # The naive policy has no verification stage, so a refused call is
            # simply the end of its involvement. It is recorded, not handled.
            continue
        if gateway.get_payment_status(payment_id).payment.state is PaymentState.CAPTURED:
            run.landed.add(payment_id)

    return run


def run_revora(dataset, failure_rate: float, rows):
    """Revora's full loop, on its own freshly reset gateway."""
    gateway, clock = seed_gateway(dataset, failure_rate)
    prior = capture_prior_states(gateway, clock, rows)

    orchestrator = AgentOrchestrator(
        gateway=gateway,
        intelligence=IntelligenceLayer(llm_client=StubLLMClient(), clock=clock),
        clock=clock,
        # Deliberately not data/batch_results.json: this benchmark must not
        # overwrite the run the dashboard reads.
        results_path=RESULTS_PATH.parent / "baseline_revora_run.json",
    )
    results = orchestrator.run_batch(
        [BatchEvent(**row["batch_event"]) for row in rows]
    )

    run = PolicyRun(name="Revora")
    for event in results.events:
        pid = event.payment_id
        # `retried` means a money-moving attempt, so it is read from what the
        # Execute stage actually did. Reading it from the outcome instead would
        # count a REQUEST_VERIFICATION status query as a retry, which is the
        # opposite of what that action is for.
        if (
            event.execution is not None
            and event.execution.action is RecommendedAction.RETRY_SOFT
            and event.execution.gateway_called
        ):
            run.retried.add(pid)
        if event.outcome is EventOutcome.RECOVERED:
            run.landed.add(pid)
        elif event.outcome is EventOutcome.BLOCKED:
            run.blocked.add(pid)
        elif event.outcome is EventOutcome.ESCALATED:
            run.escalated.add(pid)
        elif event.outcome is EventOutcome.NEEDS_REVIEW:
            run.verification_failures += 1
        # NO_ACTION is terminal and moves nothing.

    return run, prior, results


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def _assign(run: PolicyRun, payment_id: str, category: str) -> None:
    """Record one classification, keeping the assignment history.

    Writing straight into `category` would let a second assignment overwrite the
    first without trace, which is exactly the defect check (c) exists to catch.
    """
    run.assignments.append(payment_id)
    run.category[payment_id] = category


def classify(
    run: PolicyRun,
    prior: Dict[str, PriorState],
    would_be_blocked: Set[str],
) -> PolicyRun:
    """Assign every payment to exactly one category, prior state first.

    `would_be_blocked` is the set of payments a correct policy refuses. It is
    taken from Revora's own run rather than re-derived, so the naive policy is
    measured against the real engine's decisions and not a second opinion
    invented for the benchmark.
    """
    for payment_id, state in prior.items():
        # 1. Prior state wins over anything the policy did. A payment that had
        #    already succeeded cannot be recovered, whatever the call returned.
        if state.already_successful:
            _assign(run, payment_id, "already_successful_preserved_paise")
            continue

        if not state.recovery_eligible:
            # Neither successful nor recoverable. No category fits, so it is
            # surfaced rather than filed under the nearest one.
            run.unclassified.append(payment_id)
            continue

        # 2. An action a correct policy refuses, attempted anyway.
        if payment_id in run.retried and payment_id in would_be_blocked:
            _assign(run, payment_id, "incorrectly_put_at_risk_paise")
        elif payment_id in run.blocked:
            _assign(run, payment_id, "safely_blocked_paise")
        elif payment_id in run.escalated:
            _assign(run, payment_id, "escalated_paise")
        elif payment_id in run.landed:
            _assign(run, payment_id, "legitimately_recovered_paise")
        else:
            run.unclassified.append(payment_id)

    return run


def totals(run: PolicyRun, prior: Dict[str, PriorState]) -> Dict[str, int]:
    out = {name: 0 for name in CATEGORIES}
    for payment_id, category in run.category.items():
        out[category] += prior[payment_id].amount
    return out


def counts(run: PolicyRun) -> Dict[str, int]:
    out = {name: 0 for name in CATEGORIES}
    for category in run.category.values():
        out[category] += 1
    return out


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def reconcile(run: PolicyRun, prior: Dict[str, PriorState]) -> Dict[str, Any]:
    """Amount, cardinality and single-assignment, checked independently.

    A failure here is a defect in the classification logic. It is reported as
    one rather than explained away as rounding.
    """
    category_sum = sum(totals(run, prior).values())
    dataset_sum = sum(state.amount for state in prior.values())
    classified = len(run.category)

    seen: Dict[str, int] = {}
    for payment_id in run.assignments:
        seen[payment_id] = seen.get(payment_id, 0) + 1
    duplicates = sorted(pid for pid, hits in seen.items() if hits > 1)

    return {
        "amount_sum_matches": category_sum == dataset_sum,
        "category_sum_paise": category_sum,
        "dataset_sum_paise": dataset_sum,
        "classified_count": classified,
        "expected_count": len(prior),
        "count_matches": classified == len(prior),
        "duplicate_assignments": duplicates,
        "no_duplicates": not duplicates,
        "unclassified": sorted(run.unclassified),
        "passed": (
            category_sum == dataset_sum
            and classified == len(prior)
            and not duplicates
            and not run.unclassified
        ),
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def report(runs, prior, reconciliations, divergences) -> None:
    print()
    print("=" * 78)
    print("Controlled synthetic benchmark: Revora vs a naive always-retry policy")
    print("=" * 78)
    print(
        "Both policies replayed the same 500 generated incidents, from the same\n"
        "seed and reference instant, each against its own freshly reset gateway.\n"
        "These are synthetic incidents. Nothing here measures production revenue."
    )

    print()
    print("Payment state before any policy acted (established once, from gateway truth):")
    by_truth: Dict[str, int] = {}
    for state in prior.values():
        by_truth[state.truth.value] = by_truth.get(state.truth.value, 0) + 1
    for key in sorted(by_truth):
        print(f"  {key:<20} {by_truth[key]:>4}")
    eligible = sum(1 for s in prior.values() if s.recovery_eligible)
    already = sum(1 for s in prior.values() if s.already_successful)
    print(f"  {'recovery-eligible':<20} {eligible:>4}")
    print(f"  {'already successful':<20} {already:>4}")

    if divergences:
        print()
        print(
            f"Evidence/truth divergence on {len(divergences)} payment(s): the resolver read\n"
            "one state from delivered webhooks while the gateway held another. These are\n"
            "classified by truth, so a dropped success webhook cannot look like a failure:"
        )
        for pid, resolved, truth in divergences[:6]:
            print(f"  {pid:<18} resolver={resolved:<16} truth={truth}")

    print()
    print("Five-category comparison (paise, shown in rupees):")
    header = f"  {'category':<38}" + "".join(f"{r.name:>22}" for r in runs)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for category in CATEGORIES:
        line = f"  {category:<38}"
        for run in runs:
            line += f"{_rupees(totals(run, prior)[category]):>22}"
        print(line)
    print()
    line = f"  {'(payment counts)':<38}"
    print(line)
    for category in CATEGORIES:
        line = f"  {category:<38}"
        for run in runs:
            line += f"{counts(run)[category]:>22}"
        print(line)

    print()
    print("Operational counters:")
    rows_out = [
        (
            "correct recovery decisions",
            lambda r: counts(r)["legitimately_recovered_paise"],
        ),
        ("unsafe retries attempted", lambda r: counts(r)["incorrectly_put_at_risk_paise"]),
        (
            "duplicate-payment risk count",
            lambda r: sum(
                1
                for pid in r.retried
                if prior[pid].already_successful
            ),
        ),
        ("correct escalations", lambda r: len(r.escalated)),
        ("verification failures caught", lambda r: r.verification_failures),
    ]
    for label, fn in rows_out:
        line = f"  {label:<38}"
        for run in runs:
            line += f"{fn(run):>22}"
        print(line)
    print(
        "\n  The naive policy has no verification stage at all, so its verification\n"
        "  failures caught is 0 by construction, not by performing better."
    )
    print(
        "  The naive policy never blocks, so it has no safely_blocked equivalent.\n"
        "  That row is reported as zero for it rather than mirrored from Revora."
    )

    print()
    print("Reconciliation (each policy independently):")
    for run, rec in zip(runs, reconciliations):
        print(f"  {run.name}:")
        print(
            f"    (a) amount    category sum {rec['category_sum_paise']} paise "
            f"== dataset sum {rec['dataset_sum_paise']} paise  -> "
            f"{'PASS' if rec['amount_sum_matches'] else 'FAIL'}"
        )
        print(
            f"    (b) count     {rec['classified_count']} classified "
            f"== {rec['expected_count']} expected  -> "
            f"{'PASS' if rec['count_matches'] else 'FAIL'}"
        )
        print(
            f"    (c) single    {len(rec['duplicate_assignments'])} payment(s) in more "
            f"than one category  -> {'PASS' if rec['no_duplicates'] else 'FAIL'}"
        )
        if rec["unclassified"]:
            print(f"    UNCLASSIFIED: {rec['unclassified']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare Revora against a naive always-retry policy."
    )
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument(
        "--failure-rate",
        type=float,
        default=GatewaySettings().webhook_delivery_failure_rate,
    )
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    rows = dataset["events"]

    # Revora first, on its own gateway.
    revora, revora_prior, _results = run_revora(dataset, args.failure_rate, rows)

    # The naive policy, on a second gateway reset independently. Seeding is
    # repeated from scratch so neither run can consume the other's RNG stream.
    naive_gateway, naive_clock = seed_gateway(dataset, args.failure_rate)
    naive_prior = capture_prior_states(naive_gateway, naive_clock, rows)

    # The two runs must start from identical conditions or the comparison is
    # not like-for-like. Checked rather than assumed.
    mismatched = [
        pid
        for pid in revora_prior
        if revora_prior[pid].truth is not naive_prior[pid].truth
    ]
    if mismatched:
        raise SystemExit(
            "the two runs did not start from identical conditions; "
            f"{len(mismatched)} payment(s) differ, first: {mismatched[:5]}"
        )

    naive = run_naive(naive_gateway, rows)

    would_be_blocked = set(revora.blocked)
    classify(revora, revora_prior, would_be_blocked)
    classify(naive, naive_prior, would_be_blocked)

    divergences = [
        (s.payment_id, s.resolved.value, s.truth.value)
        for s in revora_prior.values()
        if s.already_successful and s.resolved not in (CanonicalState.AUTHORIZED, CanonicalState.CAPTURED)
    ]

    reconciliations = [reconcile(revora, revora_prior), reconcile(naive, naive_prior)]
    report([revora, naive], revora_prior, reconciliations, divergences)

    # Revora must never put a recovery-eligible payment at risk. Read from the
    # run's own output rather than assumed from the engine's structure.
    at_risk = counts(revora)["incorrectly_put_at_risk_paise"]
    print()
    print(
        f"Revora incorrectly_put_at_risk: {at_risk} payment(s), "
        f"{_rupees(totals(revora, revora_prior)['incorrectly_put_at_risk_paise'])}"
        f"  -> {'PASS' if at_risk == 0 else 'FAIL'}"
    )

    payload = {
        "note": (
            "Controlled synthetic benchmark comparing two policies under identical "
            "deterministic conditions. Not a measurement of production revenue."
        ),
        "dataset_count": len(rows),
        "policies": {
            run.name: {
                "amounts_paise": totals(run, revora_prior if run is revora else naive_prior),
                "counts": counts(run),
                "reconciliation": rec,
            }
            for run, rec in zip([revora, naive], reconciliations)
        },
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")

    if not all(rec["passed"] for rec in reconciliations):
        raise SystemExit(
            "reconciliation failed; the table above is not valid and must not be "
            "reported as such"
        )


if __name__ == "__main__":
    main()
