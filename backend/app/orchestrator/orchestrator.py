"""Agent orchestrator: the pipeline that turns observed payments into actions.

The loop runs in a fixed order and every stage is separable:

    Observe -> Trace -> Plan -> Validate -> Execute -> Verify

Two properties matter more than any individual stage.

**No stage passes a degraded result forward.** If a stage raises, or returns a
shape the next stage cannot consume, processing halts for that event and it is
marked NEEDS_REVIEW naming the stage that failed. Cascading a partial result is
how a pipeline reports success while having done the wrong thing.

**Verify never assumes success from a call that did not raise.** After an action
executes, the gateway is re-queried and the observed state is compared against
the state that action was supposed to produce. A mismatch is NEEDS_REVIEW, not
a recovery.

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
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status as http_status
from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger, log_event
from app.gateway.mock_gateway import (
    EntityNotFoundError,
    MockPaymentGateway,
    get_gateway,
)
from app.gateway.schemas import (
    CapturePaymentRequest,
    PaymentState,
    SimulateWebhookRequest,
    WebhookEventName,
)
from app.intelligence.llm_client import IntelligenceLayer
from app.intelligence.schemas import IntelligenceDecision, IntelligenceInput, RecommendedAction
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

#: Where the batch trace log is written so the dashboard can read it without a
#: database and without losing it when the process restarts.
#: Anchored to the repository root rather than the process working directory.
#: uvicorn is normally started from `backend/`, where a relative path would
#: resolve to a `data/` directory that does not exist -- the endpoint would then
#: return 404 for a run that is sitting on disk, and only when served rather
#: than when tested.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESULTS_PATH = REPO_ROOT / "data" / "batch_results.json"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineStage(str, Enum):
    """Named so a failure can point at the stage that produced it."""

    OBSERVE = "observe"
    TRACE = "trace"
    PLAN = "plan"
    VALIDATE = "validate"
    EXECUTE = "execute"
    VERIFY = "verify"


class EventOutcome(str, Enum):
    """Terminal disposition of one event in a batch run."""

    RECOVERED = "recovered"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    NO_ACTION = "no_action"
    NEEDS_REVIEW = "needs_review"


class BatchEvent(StrictModel):
    """One row of work: which payment to process and the compliance facts
    about it that the gateway does not hold.

    `amount` is carried through to the trace log so revenue figures can be
    summed from the log alone.
    """

    payment_id: str
    amount: int = Field(ge=0, description="Amount in paise (50000 = Rs.500)")
    currency: str = "INR"
    customer_note: Optional[str] = None

    pre_debit_notice_sent_at: Optional[datetime] = None
    mandate_ceiling: Optional[int] = Field(default=None, ge=0)
    afa_flag: Optional[bool] = None
    # Selects which AFA threshold the policy engine applies. Optional, and an
    # unrecognised value falls back to the general threshold.
    mandate_category: Optional[str] = None
    opted_out: bool = False
    retry_count: int = Field(default=0, ge=0)
    discount_amount: int = Field(default=0, ge=0)


class ExecutionRecord(StrictModel):
    """What the Execute stage actually did."""

    action: RecommendedAction
    gateway_called: bool
    calls: List[str] = Field(default_factory=list)
    expected_state: Optional[str] = None
    detail: str
    cooldown_until: Optional[datetime] = None
    # Whether the gateway call completed without error. Distinct from whether
    # the outcome was favourable -- see `reconciled`.
    succeeded: bool = True
    # REQUEST_VERIFICATION only: whether the status query found the payment had
    # actually succeeded. None for every other action.
    reconciled: Optional[bool] = None


class VerificationRecord(StrictModel):
    """The post-action re-query. `matched` is the only thing that decides
    whether an execution counts as a recovery."""

    performed: bool
    expected_state: Optional[str] = None
    observed_state: Optional[str] = None
    matched: Optional[bool] = None
    detail: str


class EventTrace(StrictModel):
    """The full decision trail for one event.

    Every field the dashboard needs is here, including `amount`, so figures can
    be summed directly from this log without re-joining the source dataset.
    """

    batch_run_id: str
    payment_id: str
    amount: int
    currency: str
    outcome: EventOutcome
    failed_stage: Optional[PipelineStage] = None
    needs_review_reason: Optional[str] = None

    # Observe / Trace
    resolved_state: Optional[CanonicalState] = None
    resolution_reason: Optional[str] = None
    resolution_confidence: Optional[float] = None
    root_cause: Optional[str] = None
    causal_chain: List[str] = Field(default_factory=list)
    trace_confidence: Optional[float] = None
    ambiguous: Optional[bool] = None
    ambiguity_reasons: List[str] = Field(default_factory=list)

    # Plan
    recommended_action: Optional[RecommendedAction] = None
    llm_called: Optional[bool] = None
    recommendation_confidence: Optional[float] = None
    reasoning: Optional[str] = None
    injection_patterns_flagged: List[str] = Field(default_factory=list)
    # What the model actually returned, when the deterministic guard replaced
    # it. `recommended_action` above is the guard's answer, so without these two
    # the audit trail shows the safe action with no record that a different one
    # was proposed. Both are None whenever the guard did not intervene.
    original_llm_action: Optional[RecommendedAction] = None
    guard_override_reason: Optional[str] = None

    # Validate
    approved: Optional[bool] = None
    final_action: Optional[RecommendedAction] = None
    blocked_reason: Optional[str] = None
    rule_id: Optional[str] = None

    # Execute / Verify
    execution: Optional[ExecutionRecord] = None
    verification: Optional[VerificationRecord] = None

    processed_at: datetime


class HumanReviewItem(StrictModel):
    """An event handed to a human, with the reasoning that got it there."""

    payment_id: str
    amount: int
    reason: str
    final_action: RecommendedAction
    root_cause: Optional[str] = None
    blocked_reason: Optional[str] = None


class BatchSummary(StrictModel):
    """Counts only. Revenue figures are summed from the per-event log, which
    carries `amount` on every entry."""

    batch_run_id: str
    started_at: datetime
    finished_at: datetime
    total_events: int
    recovered: int = 0
    blocked: int = 0
    escalated: int = 0
    needs_review: int = 0
    # Terminal no-ops. Reported separately rather than folded into another
    # bucket, since a deliberate cooldown is not a recovery or a block.
    no_action: int = 0


class BatchResults(StrictModel):
    batch_run_id: str
    summary: BatchSummary
    events: List[EventTrace] = Field(default_factory=list)
    needs_human_review: List[HumanReviewItem] = Field(default_factory=list)


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

        try:
            observation = self._observe(event)
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
                        f"payment resolved to {resolution.state.value}; it did not "
                        "fail, so no recovery is required"
                    ),
                )
                self._log_event_result(trace)
                return trace
            self._record_trace(trace, trace_result)

            recommendation = self._plan(event, trace_result)
            self._record_recommendation(trace, recommendation)

            decision = self._validate(event, recommendation, trace_result)
            self._record_decision(trace, decision)

            if not decision.approved:
                trace.outcome = EventOutcome.BLOCKED
                self._log_event_result(trace)
                return trace

            execution = self._execute(event, decision, resolution)
            trace.execution = execution

            verification = self._verify(event, execution)
            trace.verification = verification

            trace.outcome = self._outcome_for(execution, verification)
            if trace.outcome is EventOutcome.NEEDS_REVIEW:
                trace.failed_stage = PipelineStage.VERIFY
                trace.needs_review_reason = verification.detail

        except StageError as exc:
            trace.outcome = EventOutcome.NEEDS_REVIEW
            trace.failed_stage = exc.stage
            trace.needs_review_reason = exc.message
        except Exception as exc:  # a stage raised something unclassified
            trace.outcome = EventOutcome.NEEDS_REVIEW
            trace.failed_stage = PipelineStage.OBSERVE
            trace.needs_review_reason = f"unhandled error: {exc}"

        self._log_event_result(trace)
        return trace

    # -- stages ------------------------------------------------------------
    def _observe(self, event: BatchEvent) -> PaymentObservation:
        """Read the payment's merchant-visible evidence from the gateway.

        Deliberately reads `event_history` and `created_at` only. The gateway's
        own state is not consulted here -- resolving from incomplete evidence is
        the point, and Verify is where a status query belongs.
        """
        try:
            snapshot = self.gateway.get_payment_status(event.payment_id)
        except EntityNotFoundError as exc:
            raise StageError(PipelineStage.OBSERVE, str(exc)) from exc
        return PaymentObservation(
            payment_id=event.payment_id,
            created_at=snapshot.payment.created_at,
            events=list(snapshot.event_history),
            observed_at=self._clock(),
        )

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
                    retry_count=event.retry_count,
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
    ) -> ExecutionRecord:
        """Dispatch the approved action. One bounded implementation per action."""
        action = decision.final_action
        if action is RecommendedAction.RETRY_SOFT:
            return self._execute_retry_soft(event)
        if action is RecommendedAction.REQUEST_VERIFICATION:
            return self._execute_request_verification(event, resolution)
        if action is RecommendedAction.ESCALATE_HUMAN:
            return self._execute_escalate(event, decision)
        if action is RecommendedAction.NO_ACTION_COOLDOWN:
            return self._execute_cooldown(event)
        raise StageError(PipelineStage.EXECUTE, f"no implementation for action {action}")

    def _execute_retry_soft(self, event: BatchEvent) -> ExecutionRecord:
        """Re-attempt the payment through the gateway.

        A soft retry re-attempts authorization when the payment is not already
        authorized, then captures. Both calls go through the gateway's own
        endpoints; whether they are legal from the payment's current state is
        the gateway's decision, not this module's.
        """
        calls: List[str] = []
        try:
            current = self.gateway.get_payment_status(event.payment_id).payment.state
            calls.append("GET /payments/{id}/status")
            if current is not PaymentState.AUTHORIZED:
                self.gateway.simulate_webhook(
                    SimulateWebhookRequest(
                        entity_id=event.payment_id,
                        event=WebhookEventName.PAYMENT_AUTHORIZED,
                    )
                )
                calls.append("POST /webhooks/simulate (payment.authorized)")
            self.gateway.capture_payment(CapturePaymentRequest(payment_id=event.payment_id))
            calls.append("POST /payments/capture")
        except Exception as exc:
            return ExecutionRecord(
                action=RecommendedAction.RETRY_SOFT,
                gateway_called=True,
                calls=calls,
                expected_state=PaymentState.CAPTURED.value,
                detail=f"retry did not complete: {exc}",
                succeeded=False,
            )
        return ExecutionRecord(
            action=RecommendedAction.RETRY_SOFT,
            gateway_called=True,
            calls=calls,
            expected_state=PaymentState.CAPTURED.value,
            detail="re-attempted the payment and captured it",
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
            expected_state=observed.value,
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

        try:
            observed = self.gateway.get_payment_status(event.payment_id).payment.state.value
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
            if execution.reconciled and verification.matched:
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
        trace.recommendation_confidence = decision.confidence
        trace.reasoning = decision.reasoning
        trace.injection_patterns_flagged = list(
            decision.sanitization.injection_patterns_flagged
        )
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
        reason = (
            trace.needs_review_reason
            or trace.blocked_reason
            or trace.reasoning
            or "flagged for human review"
        )
        return HumanReviewItem(
            payment_id=trace.payment_id,
            amount=trace.amount,
            reason=reason,
            final_action=trace.final_action or RecommendedAction.ESCALATE_HUMAN,
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


def build_orchestrator(
    intelligence: Optional[IntelligenceLayer] = None,
) -> AgentOrchestrator:
    """Wire an orchestrator against the process-wide gateway."""
    return AgentOrchestrator(gateway=get_gateway(), intelligence=intelligence)
