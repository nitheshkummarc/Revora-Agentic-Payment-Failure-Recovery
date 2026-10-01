"""Agent orchestrator: the pipeline that turns observed payments into actions.

The loop runs in a fixed order and every stage is separable:

    Observe -> Trace -> Plan -> Validate -> Execute -> Verify

Two properties matter more than any individual stage.

**No stage passes a degraded result forward.** If a stage raises, or returns a
shape the next stage cannot consume, processing halts for that event and it is
marked NEEDS_REVIEW naming the stage that failed. Cascading a partial result is
how a pipeline reports success while having done the wrong thing.

**Verify never assumes success from a call that did not raise.** After an action
that changes gateway state executes, the gateway is re-queried and the observed
state is compared against the state that action was supposed to produce. A
mismatch is NEEDS_REVIEW, not a recovery. An action that changes nothing has
nothing to verify, and says so rather than reporting a re-read as a pass.

Observation reads only merchant-visible evidence -- delivered webhooks plus the
payment's creation time. The single sanctioned use of gateway truth is the
status query, which is exactly what REQUEST_VERIFICATION and Verify exist to
perform.

Processing is sequential by design: a batch that can be replayed step by step is
worth more during debugging than one that finishes faster.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, status as http_status

from app.core.logging import get_logger, log_event
from app.gateway.mock_gateway import (
    EntityNotFoundError,
    MockPaymentGateway,
)
from app.gateway.schemas import (
    PaymentState,
    RetryPaymentRequest,
)
from app.intelligence.llm_client import IntelligenceLayer
from app.intelligence.schemas import IntelligenceDecision, IntelligenceInput, RecommendedAction
from app.orchestrator.schemas import (
    BatchEvent,
    BatchResults,
    BatchSummary,
    EventOutcome,
    EventTrace,
    ExecutionRecord,
    HumanReviewItem,
    PipelineStage,
    VerificationRecord,
)
from app.policy.engine import PolicyEngine
from app.policy.schemas import EventContext, PolicyDecision
from app.state_machine.resolver import StateResolver
from app.state_machine.schemas import PaymentObservation, StateResolution
from app.state_machine.states import CanonicalState
from app.tracer.schemas import TraceInput, TraceResult
from app.tracer.tracer import FailurePropagationTracer, NotTraceableError

logger = get_logger("orchestrator")

#: How long a cooldown lasts when no action is taken. A local operational
#: choice with no regulatory basis; adjust freely.
DEFAULT_COOLDOWN_HOURS = 24

#: States in which a payment has actually succeeded. A verification query that
#: lands here means the ambiguity resolved favourably and no action is needed.
SETTLED_SUCCESS_STATES = frozenset({PaymentState.AUTHORIZED, PaymentState.CAPTURED})

#: States a soft retry may act from: a failed payment gets a new attempt, and
#: an authorized one is captured. Anything else is either already paid,
#: refunded, or has not failed yet.
RETRYABLE_STATES = frozenset({PaymentState.FAILED, PaymentState.AUTHORIZED})

#: Where the batch trace log is written so the dashboard can read it without a
#: database and without losing it when the process restarts.
#: Anchored to the repository root rather than the process working directory.
#: uvicorn is normally started from `backend/`, where a relative path would
#: resolve to a `data/` directory that does not exist -- the endpoint would then
#: return 404 for a run that is sitting on disk, and only when served rather
#: than when tested.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESULTS_PATH = REPO_ROOT / "data" / "batch_results.json"


class StageError(RuntimeError):
    """A stage could not produce a usable result. Carries the stage name so the
    event can be marked NEEDS_REVIEW without guessing where it failed."""

    def __init__(self, stage: PipelineStage, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentOrchestrator:
    """Runs the recovery pipeline over a batch of payments, sequentially."""

    def __init__(
        self,
        gateway: MockPaymentGateway,
        resolver: Optional[StateResolver] = None,
        tracer: Optional[FailurePropagationTracer] = None,
        intelligence: Optional[IntelligenceLayer] = None,
        policy: Optional[PolicyEngine] = None,
        clock: Optional[Callable[[], datetime]] = None,
        results_path: Optional[Path] = DEFAULT_RESULTS_PATH,
    ) -> None:
        self.gateway = gateway
        self._clock = clock or _utcnow
        self.resolver = resolver or StateResolver(clock=self._clock)
        self.tracer = tracer or FailurePropagationTracer(clock=self._clock)
        self.intelligence = intelligence or IntelligenceLayer(clock=self._clock)
        self.policy = policy or PolicyEngine(clock=self._clock)
        self.results_path = results_path

    # -- batch ------------------------------------------------------------
    def run_batch(self, events: List[BatchEvent]) -> BatchResults:
        """Process events one at a time and persist the trace log.

        A fresh `batch_run_id` is minted here rather than read from the input,
        so re-running the same dataset after a fix produces a distinguishable
        run.
        """
        # One row per payment; a repeat would act on the first row's result
        # and be counted twice.
        counts = Counter(event.payment_id for event in events)
        repeated = sorted(payment_id for payment_id, n in counts.items() if n > 1)
        if repeated:
            raise ValueError(f"batch lists payment(s) more than once: {repeated}")

        batch_run_id = str(uuid.uuid4())
        started_at = self._clock()
        traces: List[EventTrace] = []
        review: List[HumanReviewItem] = []

        for event in events:
            trace = self.process_event(event, batch_run_id)
            traces.append(trace)
            item = self._review_item(trace)
            if item is not None:
                review.append(item)

        finished_at = self._clock()
        summary = BatchSummary(
            batch_run_id=batch_run_id,
            started_at=started_at,
            finished_at=finished_at,
            total_events=len(traces),
            recovered=sum(1 for t in traces if t.outcome is EventOutcome.RECOVERED),
            blocked=sum(1 for t in traces if t.outcome is EventOutcome.BLOCKED),
            escalated=sum(1 for t in traces if t.outcome is EventOutcome.ESCALATED),
            needs_review=sum(1 for t in traces if t.outcome is EventOutcome.NEEDS_REVIEW),
            no_action=sum(1 for t in traces if t.outcome is EventOutcome.NO_ACTION),
        )
        results = BatchResults(
            batch_run_id=batch_run_id,
            summary=summary,
            events=traces,
            needs_human_review=review,
        )

        _store_batch_results(batch_run_id, results)
        self._persist(results)
        log_event(
            logger,
            "batch_complete",
            batch_run_id=batch_run_id,
            total=summary.total_events,
            recovered=summary.recovered,
            blocked=summary.blocked,
            escalated=summary.escalated,
            needs_review=summary.needs_review,
            no_action=summary.no_action,
        )
        return results

    # -- single event ------------------------------------------------------
    def process_event(self, event: BatchEvent, batch_run_id: str) -> EventTrace:
        """Run the full loop for one payment.

        Every stage is wrapped: a failure anywhere stops this event and records
        which stage produced it, rather than letting a partial result reach the
        next stage.
        """
        trace = EventTrace(
            batch_run_id=batch_run_id,
            payment_id=event.payment_id,
            amount=event.amount,
            currency=event.currency,
            outcome=EventOutcome.NEEDS_REVIEW,
            processed_at=self._clock(),
        )

        # Current stage, for attributing an unclassified exception.
        stage = PipelineStage.OBSERVE
        try:
            observation, recorded_attempts = self._observe(event)
            # Larger of the caller's count and the gateway's record.
            retry_count = max(event.retry_count, recorded_attempts)
            stage = PipelineStage.TRACE
            resolution = self._resolve(observation)
            self._record_resolution(trace, resolution)

            trace_result = self._trace(event, resolution, observation)
            if trace_result is None:
                # The payment did not fail, so there is nothing to diagnose and
                # nothing to recover. A terminal no-op, not a degraded result.
                trace.outcome = EventOutcome.NO_ACTION
                trace.execution = ExecutionRecord(
                    action=RecommendedAction.NO_ACTION_COOLDOWN,
                    gateway_called=False,
                    detail=(
                        f"payment resolved to {resolution.state.value}; no failure "
                        "has been observed yet and it is still within the silence "
                        "window, so there is nothing to recover at this point"
                        if resolution.state is CanonicalState.CREATED
                        else f"payment resolved to {resolution.state.value}; it did "
                        "not fail, so no recovery is required"
                    ),
                )
                self._log_event_result(trace)
                return trace
            self._record_trace(trace, trace_result)

            stage = PipelineStage.PLAN
            recommendation = self._plan(event, trace_result)
            self._record_recommendation(trace, recommendation)

            stage = PipelineStage.VALIDATE
            decision = self._validate(event, recommendation, trace_result, retry_count)
            self._record_decision(trace, decision)

            if not decision.approved:
                trace.outcome = EventOutcome.BLOCKED
                self._log_event_result(trace)
                return trace

            stage = PipelineStage.EXECUTE
            execution = self._execute(event, decision, resolution, retry_count)
            trace.execution = execution

            stage = PipelineStage.VERIFY
            verification = self._verify(event, execution)
            trace.verification = verification

            trace.outcome = self._outcome_for(execution, verification)
            if trace.outcome is EventOutcome.NEEDS_REVIEW:
                # An incomplete action failed in Execute; only a completed one
                # that did not match is a Verify failure.
                if not execution.succeeded:
                    trace.failed_stage = PipelineStage.EXECUTE
                    trace.needs_review_reason = execution.detail
                else:
                    trace.failed_stage = PipelineStage.VERIFY
                    trace.needs_review_reason = verification.detail

        except StageError as exc:
            trace.outcome = EventOutcome.NEEDS_REVIEW
            trace.failed_stage = exc.stage
            trace.needs_review_reason = exc.message
        except Exception as exc:  # a stage raised something unclassified
            trace.outcome = EventOutcome.NEEDS_REVIEW
            trace.failed_stage = stage
            trace.needs_review_reason = f"unhandled error: {exc}"

        self._log_event_result(trace)
        return trace

    # -- stages ------------------------------------------------------------
    def _observe(self, event: BatchEvent) -> Tuple[PaymentObservation, int]:
        """Read the payment's merchant-visible evidence from the gateway.

        Resolution uses only `event_history` and `created_at`; the gateway's
        own state is left to the status queries in Execute and Verify.

        Also checks the row's amount and currency against the payment, since
        every compliance rule runs on the row's amount, and returns the
        gateway's recorded count of recovery attempts.
        """
        try:
            snapshot = self.gateway.get_payment_status(event.payment_id)
        except EntityNotFoundError as exc:
            raise StageError(PipelineStage.OBSERVE, str(exc)) from exc
        payment = snapshot.payment
        if (payment.amount, payment.currency) != (event.amount, event.currency):
            raise StageError(
                PipelineStage.OBSERVE,
                f"batch row says {event.amount} {event.currency} but payment "
                f"{event.payment_id} is {payment.amount} {payment.currency}; "
                "compliance checks will not run on an amount the payment does not carry",
            )
        observation = PaymentObservation(
            payment_id=event.payment_id,
            created_at=snapshot.payment.created_at,
            events=list(snapshot.event_history),
            observed_at=self._clock(),
        )
        return observation, payment.recovery_attempts

    def _resolve(self, observation: PaymentObservation) -> StateResolution:
        try:
            resolution = self.resolver.resolve(observation)
        except Exception as exc:
            raise StageError(PipelineStage.TRACE, f"state resolution failed: {exc}") from exc
        if not isinstance(resolution, StateResolution):
            raise StageError(PipelineStage.TRACE, "resolver returned an unexpected shape")
        return resolution

    def _trace(
        self,
        event: BatchEvent,
        resolution: StateResolution,
        observation: PaymentObservation,
    ) -> Optional[TraceResult]:
        """Diagnose the failure. Returns None when there is no failure to
        diagnose, which is a legitimate terminal state rather than an error."""
        if not FailurePropagationTracer.is_traceable(resolution):
            return None
        try:
            result = self.tracer.trace(
                TraceInput(
                    payment_id=event.payment_id,
                    resolution=resolution,
                    events=list(observation.events),
                    traced_at=self._clock(),
                )
            )
        except NotTraceableError:
            return None
        except Exception as exc:
            raise StageError(PipelineStage.TRACE, f"tracing failed: {exc}") from exc
        if not isinstance(result, TraceResult):
            raise StageError(PipelineStage.TRACE, "tracer returned an unexpected shape")
        return result

    def _plan(self, event: BatchEvent, trace_result: TraceResult) -> IntelligenceDecision:
        """Ask for a recommendation. The recommendation layer skips the model
        entirely when the trace is ambiguous."""
        try:
            decision = self.intelligence.recommend(
                IntelligenceInput(
                    payment_id=event.payment_id,
                    trace=trace_result,
                    customer_note=event.customer_note,
                    decided_at=self._clock(),
                )
            )
        except Exception as exc:
            raise StageError(PipelineStage.PLAN, f"planning failed: {exc}") from exc
        if not isinstance(decision, IntelligenceDecision):
            raise StageError(PipelineStage.PLAN, "planner returned an unexpected shape")
        return decision

    def _validate(
        self,
        event: BatchEvent,
        recommendation: IntelligenceDecision,
        trace_result: TraceResult,
        retry_count: int,
    ) -> PolicyDecision:
        try:
            decision = self.policy.validate(
                recommendation,
                EventContext(
                    payment_id=event.payment_id,
                    amount=event.amount,
                    currency=event.currency,
                    pre_debit_notice_sent_at=event.pre_debit_notice_sent_at,
                    mandate_ceiling=event.mandate_ceiling,
                    afa_flag=event.afa_flag,
                    mandate_category=event.mandate_category,
                    opted_out=event.opted_out,
                    retry_count=retry_count,
                    discount_amount=event.discount_amount,
                    trace_confidence=trace_result.confidence,
                    evaluated_at=self._clock(),
                ),
            )
        except Exception as exc:
            raise StageError(PipelineStage.VALIDATE, f"policy validation failed: {exc}") from exc
        if not isinstance(decision, PolicyDecision):
            raise StageError(PipelineStage.VALIDATE, "policy returned an unexpected shape")
        return decision

    def _execute(
        self,
        event: BatchEvent,
        decision: PolicyDecision,
        resolution: StateResolution,
        retry_count: int,
    ) -> ExecutionRecord:
        """Dispatch the approved action. One bounded implementation per action."""
        action = decision.final_action
        if action is RecommendedAction.RETRY_SOFT:
            return self._execute_retry_soft(event, resolution, attempt=retry_count + 1)
        if action is RecommendedAction.REQUEST_VERIFICATION:
            return self._execute_request_verification(event, resolution)
        if action is RecommendedAction.ESCALATE_HUMAN:
            return self._execute_escalate(event, decision)
        if action is RecommendedAction.NO_ACTION_COOLDOWN:
            return self._execute_cooldown(event)
        raise StageError(PipelineStage.EXECUTE, f"no implementation for action {action}")

    def _execute_retry_soft(
        self, event: BatchEvent, resolution: StateResolution, attempt: int
    ) -> ExecutionRecord:
        """Retry the payment through the gateway.

        Checks gateway status first. The plan was made from delivered webhooks,
        which can be stale, so if the payment or an earlier retry attempt is
        already captured nothing is written and the event is held for review.

        Otherwise one `retry_payment` call is made under an idempotency key for
        this payment and attempt number, with the approved discount. The
        gateway charges a new attempt (or captures an existing authorization),
        and a repeated key returns the first result.
        """
        calls: List[str] = []
        idempotency_key = f"revora-retry:{event.payment_id}:{attempt}"
        try:
            status = self.gateway.get_payment_status(event.payment_id)
            calls.append("GET /payments/{id}/status")
            current = status.payment.state
            paid_attempt = next(
                (a for a in status.retry_attempts if a.state is PaymentState.CAPTURED),
                None,
            )
            if current not in RETRYABLE_STATES or paid_attempt is not None:
                already_paid = current is PaymentState.CAPTURED or paid_attempt is not None
                if paid_attempt is not None:
                    finding = (
                        f"earlier retry attempt {paid_attempt.payment_id} is already "
                        "captured, so another retry would charge the customer twice"
                    )
                elif current is PaymentState.CAPTURED:
                    finding = (
                        "the payment is already captured, so a retry would charge "
                        "the customer twice"
                    )
                else:
                    finding = f"a {current.value} payment is not retryable"
                return ExecutionRecord(
                    action=RecommendedAction.RETRY_SOFT,
                    gateway_called=True,
                    calls=calls,
                    expected_state=PaymentState.CAPTURED.value,
                    detail=(
                        f"retry stopped before any write: status {current.value}, "
                        f"evidence {resolution.state.value}; {finding}. Held for "
                        "review because the evidence the plan used was out of date."
                    ),
                    succeeded=False,
                    reconciled=already_paid,
                )
            calls.append("POST /payments/retry")
            charged = self.gateway.retry_payment(
                RetryPaymentRequest(
                    payment_id=event.payment_id,
                    idempotency_key=idempotency_key,
                    discount_amount=event.discount_amount,
                )
            ).payment
        except Exception as exc:
            return ExecutionRecord(
                action=RecommendedAction.RETRY_SOFT,
                gateway_called=True,
                calls=calls,
                expected_state=PaymentState.CAPTURED.value,
                detail=f"retry did not complete: {exc}",
                succeeded=False,
                idempotency_key=idempotency_key if "POST /payments/retry" in calls else None,
            )
        discount = event.amount - (charged.captured_amount or charged.amount)
        return ExecutionRecord(
            action=RecommendedAction.RETRY_SOFT,
            gateway_called=True,
            calls=calls,
            expected_state=PaymentState.CAPTURED.value,
            detail=(
                f"retry attempt {attempt} charged {charged.payment_id}"
                + (f" with a {discount} paise discount" if discount else "")
            ),
            idempotency_key=idempotency_key,
            charged_payment_id=charged.payment_id,
            charged_amount=charged.captured_amount,
            discount_applied=discount,
        )

    def _execute_request_verification(
        self, event: BatchEvent, resolution: StateResolution
    ) -> ExecutionRecord:
        """Query the gateway's status endpoint and reconcile.

        The query always resolves the ambiguity; what it resolves *to* decides
        the disposition. A payment that turns out to have succeeded after all
        needs no action and is reconciled. A status that confirms the failure,
        or that simply agrees with the ambiguous reading, leaves a real problem
        and goes to a human. Divergence alone is not a recovery: discovering a
        payment definitively failed is still a failure.
        """
        try:
            snapshot = self.gateway.get_payment_status(event.payment_id)
        except Exception as exc:
            return ExecutionRecord(
                action=RecommendedAction.REQUEST_VERIFICATION,
                gateway_called=True,
                calls=["GET /payments/{id}/status"],
                detail=f"status query failed: {exc}",
                succeeded=False,
            )

        observed = snapshot.payment.state
        diverged = observed.value != resolution.state.value
        reconciled = observed in SETTLED_SUCCESS_STATES

        if reconciled:
            detail = (
                f"status query returned {observed.value}; the payment had in fact "
                f"succeeded despite resolving to {resolution.state.value}. "
                "Reconciled and resolved without action."
            )
        elif diverged:
            detail = (
                f"status query returned {observed.value}, which differs from the "
                f"resolved {resolution.state.value} but confirms the payment did "
                "not succeed; escalated rather than recorded as recovered"
            )
        else:
            detail = (
                f"status query returned {observed.value}, matching the resolved "
                f"{resolution.state.value}; the ambiguity is real and the event is escalated"
            )
        return ExecutionRecord(
            action=RecommendedAction.REQUEST_VERIFICATION,
            gateway_called=True,
            calls=["GET /payments/{id}/status"],
            # Reads state rather than producing one, so nothing to expect.
            expected_state=None,
            detail=detail,
            succeeded=True,
            reconciled=reconciled,
        )

    def _execute_escalate(
        self, event: BatchEvent, decision: PolicyDecision
    ) -> ExecutionRecord:
        """Terminal. No gateway call; the event goes on the review list."""
        return ExecutionRecord(
            action=RecommendedAction.ESCALATE_HUMAN,
            gateway_called=False,
            detail="handed to a human reviewer; terminal for this run",
        )

    def _execute_cooldown(self, event: BatchEvent) -> ExecutionRecord:
        """Terminal. No gateway call; records when the cooldown lapses."""
        until = self._clock() + timedelta(hours=DEFAULT_COOLDOWN_HOURS)
        return ExecutionRecord(
            action=RecommendedAction.NO_ACTION_COOLDOWN,
            gateway_called=False,
            detail=f"no action taken; cooling down until {until.isoformat()}",
            cooldown_until=until,
        )

    def _verify(self, event: BatchEvent, execution: ExecutionRecord) -> VerificationRecord:
        """Re-query the gateway and compare against the expected state.

        A call that did not raise is not evidence the action landed. Only this
        comparison decides whether an execution counts as a recovery.
        """
        if not execution.gateway_called:
            return VerificationRecord(
                performed=False,
                detail=(
                    f"{execution.action.value} makes no gateway call, so there is "
                    "no state change to verify"
                ),
            )
        if not execution.succeeded:
            return VerificationRecord(
                performed=False,
                expected_state=execution.expected_state,
                matched=False,
                detail=f"execution did not complete, nothing to verify: {execution.detail}",
            )
        if execution.action is RecommendedAction.REQUEST_VERIFICATION:
            # The status query is the check; re-reading it would always match.
            return VerificationRecord(
                performed=False,
                detail=(
                    "REQUEST_VERIFICATION is itself the status query and changes no "
                    "state, so there is nothing to verify afterwards; its outcome "
                    "rests on what that query returned"
                ),
            )

        try:
            target = execution.charged_payment_id or event.payment_id
            observed = self.gateway.get_payment_status(target).payment.state.value
        except Exception as exc:
            raise StageError(PipelineStage.VERIFY, f"verification query failed: {exc}") from exc

        matched = observed == execution.expected_state
        return VerificationRecord(
            performed=True,
            expected_state=execution.expected_state,
            observed_state=observed,
            matched=matched,
            detail=(
                f"post-action state {observed} matches the expected "
                f"{execution.expected_state}"
                if matched
                else (
                    f"post-action state {observed} does not match the expected "
                    f"{execution.expected_state}; flagged for review rather than "
                    "recorded as recovered"
                )
            ),
        )

    # -- outcome mapping ----------------------------------------------------
    @staticmethod
    def _outcome_for(
        execution: ExecutionRecord, verification: VerificationRecord
    ) -> EventOutcome:
        if execution.action is RecommendedAction.ESCALATE_HUMAN:
            return EventOutcome.ESCALATED
        if execution.action is RecommendedAction.NO_ACTION_COOLDOWN:
            return EventOutcome.NO_ACTION
        if execution.action is RecommendedAction.REQUEST_VERIFICATION:
            if not execution.succeeded:
                # The status query itself failed, so the ambiguity is still
                # unresolved and nobody has looked at it.
                return EventOutcome.NEEDS_REVIEW
            if execution.reconciled:
                # The payment had succeeded all along; nothing needed doing.
                return EventOutcome.RECOVERED
            # The query resolved the ambiguity unfavourably: the payment really
            # did not succeed, so a human takes it from here.
            return EventOutcome.ESCALATED
        if execution.action is RecommendedAction.RETRY_SOFT:
            if not execution.succeeded or not verification.matched:
                return EventOutcome.NEEDS_REVIEW
            return EventOutcome.RECOVERED
        return EventOutcome.NEEDS_REVIEW

    # -- trace assembly -----------------------------------------------------
    @staticmethod
    def _record_resolution(trace: EventTrace, resolution: StateResolution) -> None:
        trace.resolved_state = resolution.state
        trace.resolution_reason = resolution.resolution_reason.value
        trace.resolution_confidence = resolution.resolution_confidence

    @staticmethod
    def _record_trace(trace: EventTrace, result: TraceResult) -> None:
        trace.root_cause = result.root_cause
        trace.causal_chain = list(result.causal_chain)
        trace.trace_confidence = result.confidence
        trace.ambiguous = result.ambiguous
        trace.ambiguity_reasons = list(result.ambiguity_reasons)

    @staticmethod
    def _record_recommendation(trace: EventTrace, decision: IntelligenceDecision) -> None:
        trace.recommended_action = decision.recommended_action
        trace.llm_called = decision.llm_called
        trace.short_circuit_reason = decision.short_circuit_reason
        trace.model = decision.model
        trace.recommendation_confidence = decision.confidence
        trace.reasoning = decision.reasoning
        trace.injection_patterns_flagged = list(
            decision.sanitization.injection_patterns_flagged
        )
        trace.pii_redacted = list(decision.sanitization.pii_redacted)
        trace.original_llm_action = decision.original_llm_action
        trace.guard_override_reason = decision.guard_override_reason

    @staticmethod
    def _record_decision(trace: EventTrace, decision: PolicyDecision) -> None:
        trace.approved = decision.approved
        trace.final_action = decision.final_action
        trace.blocked_reason = decision.blocked_reason
        trace.rule_id = decision.rule_id

    @staticmethod
    def _review_item(trace: EventTrace) -> Optional[HumanReviewItem]:
        """Anything a person has to look at: escalations, blocks that route to
        a human, and events that failed a stage."""
        if trace.outcome not in (
            EventOutcome.ESCALATED,
            EventOutcome.NEEDS_REVIEW,
            EventOutcome.BLOCKED,
        ):
            return None
        if (
            trace.outcome is EventOutcome.BLOCKED
            and trace.final_action is not RecommendedAction.ESCALATE_HUMAN
        ):
            return None
        # Most specific reason first. The model's reasoning comes last because
        # after a guard override or a status check it argues for something
        # that did not happen.
        verification_finding = (
            trace.execution.detail
            if trace.execution is not None
            and trace.execution.action is RecommendedAction.REQUEST_VERIFICATION
            else None
        )
        reason = (
            trace.needs_review_reason
            or trace.blocked_reason
            or trace.guard_override_reason
            or verification_finding
            or trace.reasoning
            or "flagged for human review"
        )
        return HumanReviewItem(
            payment_id=trace.payment_id,
            amount=trace.amount,
            reason=reason,
            # None when no action was decided.
            final_action=trace.final_action,
            root_cause=trace.root_cause,
            blocked_reason=trace.blocked_reason,
        )

    # -- persistence --------------------------------------------------------
    def _persist(self, results: BatchResults) -> None:
        if self.results_path is None:
            return
        path = Path(self.results_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(results.model_dump(mode="json"), indent=2), encoding="utf-8"
        )

    @staticmethod
    def _log_event_result(trace: EventTrace) -> None:
        log_event(
            logger,
            "event_processed",
            batch_run_id=trace.batch_run_id,
            payment_id=trace.payment_id,
            amount=trace.amount,
            outcome=trace.outcome.value,
            resolved_state=trace.resolved_state.value if trace.resolved_state else None,
            recommended_action=(
                trace.recommended_action.value if trace.recommended_action else None
            ),
            final_action=trace.final_action.value if trace.final_action else None,
            blocked_reason=trace.blocked_reason,
            failed_stage=trace.failed_stage.value if trace.failed_stage else None,
        )


# --------------------------------------------------------------------------
# In-memory store and read-only API
#
# Results are held in memory for the life of the process and written to disk on
# every run, so a restart does not lose the most recent batch.
# --------------------------------------------------------------------------
# Local tuning choice, not a documented figure: enough runs for a demo session
# to look back over, capped so a long-lived process doesn't grow this without
# bound. Oldest run evicted first once the cap is exceeded.
MAX_STORED_BATCH_RESULTS = 20

BATCH_RESULTS_STORE: "OrderedDict[str, BatchResults]" = OrderedDict()


def _store_batch_results(batch_run_id: str, results: BatchResults) -> None:
    BATCH_RESULTS_STORE[batch_run_id] = results
    BATCH_RESULTS_STORE.move_to_end(batch_run_id)
    while len(BATCH_RESULTS_STORE) > MAX_STORED_BATCH_RESULTS:
        BATCH_RESULTS_STORE.popitem(last=False)

router = APIRouter(tags=["orchestrator"])


def _load_persisted(batch_run_id: str) -> Optional[BatchResults]:
    path = Path(DEFAULT_RESULTS_PATH)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("batch_run_id") != batch_run_id:
        return None
    return BatchResults.model_validate(payload)


@router.get("/api/batch-results/{batch_run_id}", response_model=BatchResults)
def batch_results(batch_run_id: str) -> BatchResults:
    results = BATCH_RESULTS_STORE.get(batch_run_id) or _load_persisted(batch_run_id)
    if results is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"no batch run {batch_run_id}",
        )
    return results
