"""Schemas for failure propagation tracing.

The tracer's input is the complete StateResolution, not just the final
CanonicalState. Two payments can reach the same state by very different routes
-- a clean single `payment.failed`, versus a chain where events had to be
ignored as unreachable -- and the root cause, ambiguity flag and confidence all
have to reflect that difference. Carrying only the state would discard the
distinction at exactly the layer whose job is to explain it.

Every model sets extra="forbid".
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

    Like PaymentObservation, this schema has no field that can carry the
    gateway's internal state: the tracer diagnoses from evidence plus the
    resolver's reading of it, nothing else.
    """

    payment_id: str
    # The complete resolution, including ignored_event_ids and
    # resolution_reason. Deliberately not narrowed to `.state`.
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

    `root_cause`, `causal_chain`, `confidence` and `ambiguous` are the contract;
    everything below them is audit detail allowing the verdict to be
    reconstructed.

    `ambiguous` is the single gate forcing a status query before any recovery
    action. There is deliberately no second needs-status-check flag: one gate,
    one meaning, so the two cannot drift apart.
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

    # --- carried through from resolution, so the audit trail is continuous ---
    resolved_state: CanonicalState
    resolution_reason: str
    ignored_event_ids: List[str] = Field(default_factory=list)

    # --- chain detail ---
    missing_sequences: List[int] = Field(default_factory=list)
    causal_hops: List[CausalHop] = Field(default_factory=list)
    grounded_error: Optional[ErrorObject] = None

    traced_at: datetime
