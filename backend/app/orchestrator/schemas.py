"""Orchestrator API contract: the schema classes, kept apart from the pipeline
logic that fills them in.

Every model uses `extra="forbid"`, like the rest of Revora.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.intelligence.schemas import RecommendedAction
from app.state_machine.states import CanonicalState


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
    # INR only: the compliance thresholds are rupee figures.
    currency: Literal["INR"] = "INR"
    customer_note: Optional[str] = None

    # Timezone-aware only; the 24h rule subtracts it from an aware time.
    pre_debit_notice_sent_at: Optional[AwareDatetime] = None
    mandate_ceiling: Optional[int] = Field(default=None, ge=0)
    afa_flag: Optional[bool] = None
    # Selects which AFA threshold the policy engine applies. Optional, and an
    # unrecognised value falls back to the general threshold.
    mandate_category: Optional[str] = None
    opted_out: bool = False
    # Attempts already made, as the caller knows them. The policy uses the
    # larger of this and the gateway's recorded count.
    retry_count: int = Field(default=0, ge=0)
    discount_amount: int = Field(default=0, ge=0)


class ExecutionRecord(StrictModel):
    """What the Execute stage actually did."""

    action: RecommendedAction
    gateway_called: bool
    # Gateway operations performed, named by their HTTP route. Called
    # in-process, not over HTTP.
    calls: List[str] = Field(default_factory=list)
    expected_state: Optional[str] = None
    detail: str
    cooldown_until: Optional[datetime] = None
    # RETRY_SOFT only: the key the attempt was sent under.
    idempotency_key: Optional[str] = None
    # RETRY_SOFT only: the payment that was charged (a new attempt, or the
    # original when an authorization was captured), the amount captured, and
    # the discount taken off it. All in paise.
    charged_payment_id: Optional[str] = None
    charged_amount: Optional[int] = None
    discount_applied: Optional[int] = None
    # Whether the action ran to completion (not whether it went well; see
    # `reconciled`). A retry that stops at its status check did not complete.
    succeeded: bool = True
    # Whether a status query found the payment had already succeeded. Set by
    # REQUEST_VERIFICATION, and by RETRY_SOFT when it stops on a captured
    # payment.
    reconciled: Optional[bool] = None


class VerificationRecord(StrictModel):
    """The post-action re-query. For an action that changes gateway state
    (RETRY_SOFT), `matched` is the only thing that decides whether it counts as
    a recovery. Actions that change nothing -- including REQUEST_VERIFICATION,
    whose status query is itself the check -- record `performed=False`."""

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
    # Why no model answer was used when llm_called is False: "tracer_ambiguous"
    # or a fail-safe reason such as "llm_call_failed".
    short_circuit_reason: Optional[str] = None
    # The model that produced the recommendation; None when none did.
    model: Optional[str] = None
    recommendation_confidence: Optional[float] = None
    reasoning: Optional[str] = None
    injection_patterns_flagged: List[str] = Field(default_factory=list)
    # Kinds of personal data redacted from the note (e.g. "phone").
    pii_redacted: List[str] = Field(default_factory=list)
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
    # None when the event stopped before an action was decided.
    final_action: Optional[RecommendedAction] = None
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
