"""Schemas for the FailurePropagationTracer (Module 3).

The tracer's input is the FULL StateResolution from Module 2, not just the
final CanonicalState. Two payments can resolve to the same state by very
different routes -- a clean single `payment.failed`, versus a chain where
events had to be ignored as unreachable -- and the tracer's root cause,
ambiguity flag and confidence all have to reflect that difference. Carrying
only the state would throw that distinction away at exactly the layer whose
job is to explain it.

Every model sets extra="forbid", same rationale as Modules 1 and 2.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.gateway.schemas import ErrorObject, WebhookEvent
from app.state_machine.schemas import StateResolution
from app.state_machine.states import CanonicalState


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TraceInput(StrictModel):
    """Everything the tracer is allowed to see about one payment.

    Like Module 2's PaymentObservation, this schema has no field that can
    carry the gateway's internal ground-truth state -- the tracer diagnoses
    from evidence plus the resolver's reading of it, nothing else.
    """

    payment_id: str
    # The complete Module 2 output, including ignored_event_ids and
    # resolution_reason. Not narrowed to `.state`.
    resolution: StateResolution
    # The linked event chain: webhook attempts, retries, gateway responses.
    events: List[WebhookEvent] = Field(default_factory=list)
    traced_at: Optional[datetime] = None


class CausalHop(StrictModel):
    """One node on the causal path, root-first."""

    node_id: str
    event_id: str
    event: str
    sequence: int
    delivery_attempt: int
    occurred_at: datetime
    implied_state: Optional[CanonicalState] = None
    is_duplicate_delivery: bool = False
    has_error_object: bool = False


class TraceResult(StrictModel):
    """The tracer's output.

    The four fields GROUND_TRUTH.md Day 3-4 specifies verbatim are
    `root_cause`, `causal_chain`, `confidence` and `ambiguous`. Everything
    below those is audit-trail detail so a judge can reconstruct exactly how
    the verdict was reached.

    `ambiguous` is the single gate for "force a status-endpoint query before
    any recovery action". There is deliberately no second, separate
    needs-status-check flag on this model -- one gate, one meaning.
    """

    payment_id: str

    # --- the four required fields ---
    root_cause: str
    causal_chain: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguous: bool

    # --- why it was called ambiguous ---
    ambiguity_reasons: List[str] = Field(default_factory=list)

    # --- confidence formula inputs, exposed so the score is reproducible ---
    chain_completeness: float = Field(ge=0.0, le=1.0)
    error_grounding: float = Field(ge=0.0, le=1.0)
    inherited_resolution_confidence: float = Field(ge=0.0, le=1.0)

    # --- carried through from Module 2, so the audit trail is continuous ---
    resolved_state: CanonicalState
    resolution_reason: str
    ignored_event_ids: List[str] = Field(default_factory=list)

    # --- chain detail ---
    missing_sequences: List[int] = Field(default_factory=list)
    causal_hops: List[CausalHop] = Field(default_factory=list)
    grounded_error: Optional[ErrorObject] = None

    traced_at: datetime
