"""Failure propagation tracer tests.

The test the requires explicitly is
`test_incomplete_chain_is_ambiguous_and_does_not_fabricate_a_root_cause`, plus
its stronger sibling where the *failure event itself* is the missing one.

Also pinned here: what resolution hands forward. A chain where events were
ignored during resolution must not trace the same as a clean chain that happens
to resolve to the same state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import GatewaySettings
from app.gateway.mock_gateway import MockPaymentGateway
from app.gateway.schemas import (
    ChaosConfig,
    ChaosMode,
    CreatePaymentRequest,
    ErrorObject,
    ErrorSource,
    FailPaymentRequest,
    WebhookEvent,
    WebhookEventName,
)
from app.state_machine.resolver import StateResolver
from app.state_machine.schemas import PaymentObservation, ResolutionRule
from app.state_machine.states import CanonicalState
from app.tracer.schemas import TraceInput, TraceResult
from app.tracer.tracer import (
    CONFIDENCE_AMBIGUITY_THRESHOLD,
    PENALTY_PER_IGNORED_EVENT,
    FailurePropagationTracer,
    NotTraceableError,
)

START = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> datetime:
        self.t = self.t + timedelta(seconds=seconds)
        return self.t


# A documented Razorpay error object, reproduced verbatim.
OTP_ERROR = ErrorObject(
    code="BAD_REQUEST_ERROR",
    description="Payment failed due to incorrect OTP",
    field="otp",
    source=ErrorSource.CUSTOMER,
    step="payment_authentication",
    reason="incorrect_otp",
)


def make_event(
    event: WebhookEventName,
    *,
    sequence: int,
    occurred_offset: float,
    error: ErrorObject | None = None,
    delivery_attempt: int = 1,
    payment_id: str = "pay_1",
) -> WebhookEvent:
    occurred = START + timedelta(seconds=occurred_offset)
    return WebhookEvent(
        event_id=f"evt_{payment_id}_{sequence}",
        entity_type="payment",
        entity_id=payment_id,
        event=event,
        sequence=sequence,
        occurred_at=occurred,
        webhook_sent_at=occurred,
        webhook_received_at=occurred,
        delivery_attempt=delivery_attempt,
        is_duplicate_delivery=delivery_attempt > 1,
        resulting_state="UNUSED",
        error=error,
    )


@pytest.fixture()
def tracer() -> FailurePropagationTracer:
    return FailurePropagationTracer(clock=FakeClock())


@pytest.fixture()
def resolver() -> StateResolver:
    return StateResolver(clock=FakeClock())


def resolve_and_trace(
    resolver: StateResolver,
    tracer: FailurePropagationTracer,
    events: list[WebhookEvent],
    *,
    payment_id: str = "pay_1",
    observed_offset: float = 120,
) -> TraceResult:
    """Run the real resolve-then-trace pipeline."""
    resolution = resolver.resolve(
        events,
        payment_id=payment_id,
        created_at=START,
        observed_at=START + timedelta(seconds=observed_offset),
    )
    return tracer.trace(
        TraceInput(
            payment_id=payment_id,
            resolution=resolution,
            events=events,
            traced_at=START + timedelta(seconds=observed_offset),
        )
    )


# --------------------------------------------------------------------------
# Output contract
# --------------------------------------------------------------------------
def test_output_has_the_four_required_fields(resolver, tracer):
    """The output shape is a fixed contract."""
    events = [make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR)]
    result = resolve_and_trace(resolver, tracer, events)
    payload = result.model_dump(mode="json")
    for field in ("root_cause", "causal_chain", "confidence", "ambiguous"):
        assert field in payload
    assert isinstance(payload["root_cause"], str)
    assert isinstance(payload["causal_chain"], list)
    assert isinstance(payload["confidence"], float)
    assert isinstance(payload["ambiguous"], bool)


def test_tracer_refuses_to_diagnose_a_payment_that_did_not_fail(resolver, tracer):
    """Returning a placeholder cause for a CAPTURED payment would put a
    fabricated diagnosis into the audit trail."""
    events = [
        make_event(WebhookEventName.PAYMENT_AUTHORIZED, sequence=1, occurred_offset=1),
        make_event(WebhookEventName.PAYMENT_CAPTURED, sequence=2, occurred_offset=2),
    ]
    resolution = resolver.resolve(
        events, payment_id="pay_1", created_at=START, observed_at=START + timedelta(seconds=10)
    )
    assert resolution.state is CanonicalState.CAPTURED
    assert FailurePropagationTracer.is_traceable(resolution) is False
    with pytest.raises(NotTraceableError, match="will not invent a cause"):
        tracer.trace(TraceInput(payment_id="pay_1", resolution=resolution, events=events))


# --------------------------------------------------------------------------
# Root cause is quoted from the real error fields, never paraphrased
# --------------------------------------------------------------------------
def test_root_cause_quotes_source_step_and_reason_verbatim(resolver, tracer):
    """literal values, not an LLM-style
    paraphrase, so the cause stays greppable back to the real event."""
    events = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR)
    ]
    result = resolve_and_trace(resolver, tracer, events)

    assert result.root_cause == (
        "Failure at step: payment_authentication, source: customer, reason: incorrect_otp"
    )
    assert "payment_authentication" in result.root_cause
    assert "customer" in result.root_cause
    assert "incorrect_otp" in result.root_cause
    assert result.ambiguous is False
    assert result.confidence == 1.0


def test_root_cause_uses_the_bank_source_verbatim_too(resolver, tracer):
    """`insufficient_funds` and source `bank` are real
    values; the format must carry whatever the event actually holds."""
    bank_error = ErrorObject(
        code="BAD_REQUEST_ERROR",
        description="Payment failed",
        field=None,
        source=ErrorSource.BANK,
        step="payment_authentication",
        reason="insufficient_funds",
    )
    events = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=bank_error)
    ]
    result = resolve_and_trace(resolver, tracer, events)
    assert result.root_cause == (
        "Failure at step: payment_authentication, source: bank, reason: insufficient_funds"
    )
    assert result.grounded_error is not None
    assert result.grounded_error.reason == "insufficient_funds"


def test_no_parallel_taxonomy_is_invented(resolver, tracer):
    """The tracer must not classify into categories of its own -- the only
    cause vocabulary is what the event's error object carries."""
    events = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR)
    ]
    result = resolve_and_trace(resolver, tracer, events)
    assert result.grounded_error == OTP_ERROR
    # Every substantive token in the cause traces to a field on the error object.
    for token in ("payment_authentication", "customer", "incorrect_otp"):
        assert token in (OTP_ERROR.step, OTP_ERROR.source.value, OTP_ERROR.reason)


# --------------------------------------------------------------------------
# Causal chain: reverse BFS over a real graph, not just the last error
# --------------------------------------------------------------------------
def test_causal_chain_is_multi_hop_and_root_first(resolver, tracer):
    events = [
        make_event(WebhookEventName.PAYMENT_AUTHORIZED, sequence=1, occurred_offset=1),
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=2, occurred_offset=9, error=OTP_ERROR),
    ]
    result = resolve_and_trace(resolver, tracer, events)

    assert result.causal_chain == ["evt_pay_1_1", "evt_pay_1_2"]
    assert [hop.event for hop in result.causal_hops] == [
        "payment.authorized",
        "payment.failed",
    ]
    assert result.causal_hops[0].implied_state is CanonicalState.AUTHORIZED
    assert result.causal_hops[-1].implied_state is CanonicalState.FAILED
    assert result.ambiguous is False


def test_retry_delivery_attempts_are_distinct_causal_nodes(resolver, tracer):
    """A redelivery is a webhook attempt that really happened, so it is its own
    node on the causal path even though it collapses to one event_id."""
    first = make_event(
        WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR
    )
    retry = make_event(
        WebhookEventName.PAYMENT_FAILED,
        sequence=1,
        occurred_offset=5,
        error=OTP_ERROR,
        delivery_attempt=2,
    )
    result = resolve_and_trace(resolver, tracer, [first, retry])

    assert result.causal_chain == ["evt_pay_1_1"]
    assert len(result.causal_hops) == 2
    assert [hop.delivery_attempt for hop in result.causal_hops] == [1, 2]
    assert result.causal_hops[1].is_duplicate_delivery is True


# --------------------------------------------------------------------------
# REQUIRED: an incomplete chain is ambiguous and fabricates nothing
# --------------------------------------------------------------------------
def test_incomplete_chain_is_ambiguous_and_does_not_fabricate_a_root_cause(
    resolver, tracer
):
    """The chain here has a hole: sequences 1 and 3 are present, 2 is gone
    (crash, log loss). Returning a shorter chain that looks complete is exactly
    the dropped-Spark-RCA mistake. The tracer must report the hole.
    """
    events = [
        make_event(WebhookEventName.PAYMENT_AUTHORIZED, sequence=1, occurred_offset=1),
        # sequence 2 is deliberately missing -- lost telemetry
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=3, occurred_offset=20, error=OTP_ERROR),
    ]
    result = resolve_and_trace(resolver, tracer, events)

    assert result.ambiguous is True
    assert result.missing_sequences == [2]
    assert result.chain_completeness == pytest.approx(2 / 3, abs=1e-3)
    assert result.confidence < 1.0

    gap_reasons = [r for r in result.ambiguity_reasons if r.startswith("chain_gap_missing_telemetry")]
    assert gap_reasons, "the gap must be named in ambiguity_reasons"
    assert "[2]" in gap_reasons[0]

    # It still quotes the real error it DID see -- but never claims the chain
    # was complete.
    assert result.root_cause.startswith("Failure at step: payment_authentication")
    assert "chain incomplete, missing sequence(s) [2]" in result.root_cause


def test_missing_failure_event_produces_no_invented_cause(resolver, tracer):
    """The stronger case: the failure event itself is the missing telemetry.

    There is no error object anywhere, so there is no source/step/reason to
    quote. The tracer must say so rather than manufacture a plausible triplet.
    """
    # A payment.failed arrived, but carrying no error object at all -- the
    # telemetry that would name source/step/reason is the part that was lost.
    failed_only = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=2, occurred_offset=30, error=None)
    ]
    resolution = resolver.resolve(
        failed_only,
        payment_id="pay_1",
        created_at=START,
        observed_at=START + timedelta(seconds=600),
    )
    assert resolution.state is CanonicalState.FAILED

    result = tracer.trace(
        TraceInput(payment_id="pay_1", resolution=resolution, events=failed_only)
    )

    assert result.ambiguous is True
    assert result.grounded_error is None
    assert result.error_grounding == 0.0
    assert result.confidence == 0.0
    assert result.root_cause.startswith("Undetermined root cause")
    assert "status-endpoint query is required" in result.root_cause
    # Nothing that looks like a fabricated diagnosis.
    assert "Failure at step:" not in result.root_cause
    assert any(
        r.startswith("no_error_object_on_failure") for r in result.ambiguity_reasons
    )


def test_incomplete_error_triplet_is_ambiguous(resolver, tracer):
    """A blank `reason` means the triplet is not fully grounded."""
    partial = ErrorObject(
        code="BAD_REQUEST_ERROR",
        description="Payment failed",
        field=None,
        source=ErrorSource.GATEWAY,
        step="payment_authentication",
        reason="   ",
    )
    events = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=partial)
    ]
    result = resolve_and_trace(resolver, tracer, events)

    assert result.ambiguous is True
    assert result.error_grounding == pytest.approx(2 / 3, abs=1e-3)
    assert any(r.startswith("incomplete_error_triplet") for r in result.ambiguity_reasons)


# --------------------------------------------------------------------------
# Continuity: an inconsistent chain must not trace like a clean one
# --------------------------------------------------------------------------
def test_inconsistent_chain_traces_differently_from_a_clean_chain(resolver, tracer):
    """Both payments resolve to FAILED. One is clean; the other had an event
    ignored during resolution. Root cause, ambiguity and confidence must all
    distinguish them -- otherwise the tracer has thrown away the distinction
    the resolver went to the trouble of recording."""
    clean = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR)
    ]
    inconsistent = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR),
        # A capture with no preceding authorization: unreachable, so resolution
        # ignores it -- evidence nobody has explained.
        make_event(WebhookEventName.PAYMENT_CAPTURED, sequence=2, occurred_offset=9),
    ]

    clean_result = resolve_and_trace(resolver, tracer, clean)
    messy_result = resolve_and_trace(resolver, tracer, inconsistent)

    # Same resolved state...
    assert clean_result.resolved_state is CanonicalState.FAILED
    assert messy_result.resolved_state is CanonicalState.FAILED

    # ...but the tracer must not treat them the same.
    assert clean_result.ambiguous is False
    assert messy_result.ambiguous is True
    assert messy_result.confidence < clean_result.confidence
    assert messy_result.ignored_event_ids == ["evt_pay_1_2"]
    assert messy_result.resolution_reason == "inconsistent_event_chain"
    assert any(
        r.startswith("events_ignored_during_resolution")
        for r in messy_result.ambiguity_reasons
    )
    # The difference is visible in the root cause string itself.
    assert "ignored during state resolution" in messy_result.root_cause
    assert "ignored during state resolution" not in clean_result.root_cause


def test_ignored_events_penalise_confidence_by_the_documented_amount(resolver, tracer):
    inconsistent = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR),
        make_event(WebhookEventName.PAYMENT_CAPTURED, sequence=2, occurred_offset=9),
    ]
    result = resolve_and_trace(resolver, tracer, inconsistent)

    expected = (
        result.chain_completeness
        * result.error_grounding
        * result.inherited_resolution_confidence
        - PENALTY_PER_IGNORED_EVENT * len(result.ignored_event_ids)
    )
    assert result.confidence == pytest.approx(round(max(0.0, expected), 4))


def test_tracer_inherits_resolver_uncertainty(resolver, tracer):
    """The tracer cannot be more certain about *why* than the resolver was
    about *what*."""
    events = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR),
    ]
    result = resolve_and_trace(resolver, tracer, events)
    assert result.inherited_resolution_confidence == 1.0
    assert result.confidence <= result.inherited_resolution_confidence


# --------------------------------------------------------------------------
# PENDING_WEBHOOK / silence
# --------------------------------------------------------------------------
def test_pending_webhook_silence_is_always_ambiguous(resolver, tracer):
    """The headline separation: 'genuinely failed, safe to act' versus
    'ambiguous, needs status check'."""
    resolution = resolver.resolve(
        [], payment_id="pay_silent", created_at=START, observed_at=START + timedelta(seconds=600)
    )
    assert resolution.state is CanonicalState.PENDING_WEBHOOK

    result = tracer.trace(
        TraceInput(payment_id="pay_silent", resolution=resolution, events=[])
    )

    assert result.ambiguous is True
    assert result.confidence == 0.0
    assert result.causal_chain == []
    assert result.root_cause.startswith("Undetermined root cause for state PENDING_WEBHOOK")
    assert any(r.startswith("no_delivered_events") for r in result.ambiguity_reasons)
    assert any(r.startswith("pending_webhook_state") for r in result.ambiguity_reasons)


def test_confidence_threshold_is_a_backstop_not_the_only_gate(resolver, tracer):
    """A gapped chain can still score above the confidence threshold; the
    structural gap rule is what catches it."""
    events = [
        make_event(WebhookEventName.PAYMENT_AUTHORIZED, sequence=1, occurred_offset=1),
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=3, occurred_offset=20, error=OTP_ERROR),
    ]
    result = resolve_and_trace(resolver, tracer, events)
    assert result.confidence > CONFIDENCE_AMBIGUITY_THRESHOLD
    assert result.ambiguous is True
    assert not any(
        r.startswith("confidence_below_threshold") for r in result.ambiguity_reasons
    )


# --------------------------------------------------------------------------
# Determinism / architectural boundary
# --------------------------------------------------------------------------
def test_tracer_module_makes_no_llm_or_gateway_calls():
    """Hard architectural boundary: the tracer is fully deterministic."""
    import pathlib

    source = pathlib.Path("backend/app/tracer/tracer.py").read_text(encoding="utf-8")
    for forbidden in ("groq", "genai", "openai", "httpx", "requests", "MockPaymentGateway"):
        assert forbidden not in source, f"tracer must not reference {forbidden}"


def test_tracing_is_deterministic(resolver, tracer):
    events = [
        make_event(WebhookEventName.PAYMENT_AUTHORIZED, sequence=1, occurred_offset=1),
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=2, occurred_offset=9, error=OTP_ERROR),
    ]
    first = resolve_and_trace(resolver, tracer, events)
    second = resolve_and_trace(resolver, tracer, events)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# --------------------------------------------------------------------------
# End-to-end against the real fault injector
# --------------------------------------------------------------------------
def _pipeline(chaos: ChaosConfig, advance: float, payment_id: str = "pay_e2e"):
    clock = FakeClock()
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=0.0), clock=clock
    )
    gateway.create_payment(CreatePaymentRequest(payment_id=payment_id, amount=250000))
    gateway.fail_payment(FailPaymentRequest(payment_id=payment_id, chaos=chaos))
    clock.advance(advance)

    status = gateway.get_payment_status(payment_id)
    observation = PaymentObservation(
        payment_id=payment_id,
        created_at=status.payment.created_at,
        events=list(status.event_history),
        observed_at=clock(),
    )
    resolution = StateResolver(clock=clock).resolve(observation)
    return gateway, resolution, observation, clock


def test_end_to_end_genuine_failure_is_safe_to_act_on():
    """A clean failure from the real injector: grounded, complete, not
    ambiguous. This is the 'safe to act' side of the headline demo."""
    gateway, resolution, observation, clock = _pipeline(ChaosConfig(), advance=10)
    assert resolution.state is CanonicalState.FAILED

    tracer = FailurePropagationTracer(clock=clock)
    result = tracer.trace(
        TraceInput(
            payment_id="pay_e2e", resolution=resolution, events=observation.events
        )
    )

    assert result.ambiguous is False
    assert result.confidence == 1.0
    # The gateway fell back to its documented example error object.
    assert result.root_cause == (
        "Failure at step: payment_authentication, source: customer, reason: incorrect_otp"
    )


def test_end_to_end_silent_drop_is_ambiguous_needs_status_check():
    """The other side of the headline demo: the gateway knows it FAILED, but
    from delivered evidence the tracer must refuse to diagnose."""
    gateway, resolution, observation, clock = _pipeline(
        ChaosConfig(modes=[ChaosMode.SILENT_DROP]), advance=600, payment_id="pay_e2e"
    )
    assert resolution.state is CanonicalState.PENDING_WEBHOOK
    assert resolution.resolution_reason is ResolutionRule.SILENCE_THRESHOLD_EXCEEDED

    tracer = FailurePropagationTracer(clock=clock)
    result = tracer.trace(
        TraceInput(
            payment_id="pay_e2e", resolution=resolution, events=observation.events
        )
    )

    assert result.ambiguous is True
    assert result.confidence == 0.0
    assert "Undetermined root cause" in result.root_cause
    # The tracer never claims the failure the gateway privately knows about.
    assert "incorrect_otp" not in result.root_cause


def test_end_to_end_flip_is_not_traceable_because_it_recovered():
    """A payment that flipped back to AUTHORIZED did not fail -- asking the
    tracer to explain it should raise, not produce a cause."""
    gateway, resolution, observation, clock = _pipeline(
        ChaosConfig(modes=[ChaosMode.FAILED_AUTHORIZED_FLIP], flip_after_seconds=30),
        advance=45,
        payment_id="pay_e2e",
    )
    assert resolution.state is CanonicalState.AUTHORIZED
    tracer = FailurePropagationTracer(clock=clock)
    with pytest.raises(NotTraceableError):
        tracer.trace(
            TraceInput(
                payment_id="pay_e2e", resolution=resolution, events=observation.events
            )
        )


def test_end_to_end_duplicate_delivery_does_not_create_a_false_gap():
    """A duplicate webhook must not look like missing telemetry."""
    clock = FakeClock()
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=0.0), clock=clock
    )
    gateway.create_payment(CreatePaymentRequest(payment_id="pay_dup", amount=50000))
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_dup",
            chaos=ChaosConfig(modes=[ChaosMode.DUPLICATE_WEBHOOK], duplicate_after_seconds=1),
        )
    )
    clock.advance(5)

    status = gateway.get_payment_status("pay_dup")
    resolution = StateResolver(clock=clock).resolve(
        PaymentObservation(
            payment_id="pay_dup",
            created_at=status.payment.created_at,
            events=list(status.event_history),
            observed_at=clock(),
        )
    )
    result = FailurePropagationTracer(clock=clock).trace(
        TraceInput(
            payment_id="pay_dup",
            resolution=resolution,
            events=list(status.event_history),
        )
    )

    assert result.missing_sequences == []
    assert result.chain_completeness == 1.0
    assert result.ambiguous is False


def test_single_ignored_event_lands_exactly_on_the_confidence_threshold(resolver, tracer):
    """Boundary pin: n=1 sits exactly on CONFIDENCE_AMBIGUITY_THRESHOLD.

    n=1 sits exactly on CONFIDENCE_AMBIGUITY_THRESHOLD -- safety currently
    comes from the structural rule alone at this point, not the confidence
    check; if PENALTY_ILLEGAL_TRANSITION or this threshold ever changes,
    re-verify this case.

    The arithmetic: resolution deducts PENALTY_ILLEGAL_TRANSITION (0.25) for
    the one ignored event, giving inherited = 0.75. The tracer then computes
    1.0 * 1.0 * 0.75 - PENALTY_PER_IGNORED_EVENT (0.15) = 0.60 exactly. Since
    the gate is `confidence < threshold`, 0.60 < 0.60 is False, so
    `confidence_below_threshold` does NOT fire -- the verdict rests entirely on
    `events_ignored_during_resolution`.
    """
    events = [
        make_event(WebhookEventName.PAYMENT_FAILED, sequence=1, occurred_offset=5, error=OTP_ERROR),
        # A capture with no preceding authorization: unreachable, so resolution
        # ignores exactly one event.
        make_event(WebhookEventName.PAYMENT_CAPTURED, sequence=2, occurred_offset=9),
    ]
    result = resolve_and_trace(resolver, tracer, events)

    assert len(result.ignored_event_ids) == 1
    assert result.inherited_resolution_confidence == pytest.approx(0.75)
    assert result.chain_completeness == 1.0
    assert result.error_grounding == 1.0

    # Exactly on the boundary, not below it.
    assert result.confidence == pytest.approx(CONFIDENCE_AMBIGUITY_THRESHOLD)
    assert not (result.confidence < CONFIDENCE_AMBIGUITY_THRESHOLD)

    # Ambiguous, but NOT because of the confidence check.
    assert result.ambiguous is True
    assert not any(
        r.startswith("confidence_below_threshold") for r in result.ambiguity_reasons
    ), "the confidence gate must not be what is protecting this case"
    assert any(
        r.startswith("events_ignored_during_resolution") for r in result.ambiguity_reasons
    ), "the structural rule is the only thing protecting this case"
