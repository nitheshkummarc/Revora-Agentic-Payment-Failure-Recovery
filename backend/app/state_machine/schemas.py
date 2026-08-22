"""Schemas for the StateResolver (Module 2).

The input schema (`PaymentObservation`) is the trust boundary of this module.
It deliberately carries NO field that can hold the gateway's internal
ground-truth state: only delivered webhook events plus the two timestamps
needed to evaluate the silence threshold. The resolver therefore cannot peek
at gateway truth even by accident -- resolving from incomplete evidence is the
whole premise, and a field that could carry the answer would defeat it.

Every model sets extra="forbid", same rationale as Module 1: a shape mismatch
must throw rather than silently pass through.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.gateway.schemas import WebhookEvent
from app.state_machine.states import CanonicalState


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolutionRule(str, Enum):
    """Which rule decided the state.

    The three values named in the Module 2 prompt appear here verbatim
    (`clean_single_event`, `late_authorization_flip`,
    `silence_threshold_exceeded`) so an auditor grepping for them finds them in
    `resolution_reason`.
    """

    CLEAN_SINGLE_EVENT = "clean_single_event"
    ORDERED_EVENT_CHAIN = "ordered_event_chain"
    LATE_AUTHORIZATION_FLIP = "late_authorization_flip"
    SILENCE_THRESHOLD_EXCEEDED = "silence_threshold_exceeded"
    WITHIN_SILENCE_THRESHOLD = "within_silence_threshold"
    # At least one event implied a transition that is not reachable from the
    # state the evidence had reached (e.g. a capture with no authorization).
    # Reported as its own rule so the audit trail never calls an inconsistent
    # chain "clean".
    INCONSISTENT_EVENT_CHAIN = "inconsistent_event_chain"


class ResolutionLogEntry(StrictModel):
    """One audit-trail line. GROUND_TRUTH.md Day 8-10 requires every stage's
    reasoning to be reconstructable later; the flip entry in particular is
    called out by the Module 2 prompt as mattering for the audit trail."""

    rule: str
    message: str
    event_ids: List[str] = Field(default_factory=list)
    at: datetime


class PaymentObservation(StrictModel):
    """Everything the resolver is allowed to see about one payment.

    `events` are DELIVERED webhooks only -- the merchant-visible evidence. A
    webhook that was silently dropped does not appear here, which is exactly
    what makes silence detectable.
    """

    payment_id: str
    created_at: datetime
    events: List[WebhookEvent] = Field(default_factory=list)
    # When the observation was taken. Defaults to "now" at resolve time.
    observed_at: Optional[datetime] = None


class StateResolution(StrictModel):
    """The resolver's output."""

    payment_id: str
    state: CanonicalState
    needs_status_check: bool = False

    # Required by Module 2 prompt requirement 3.
    resolution_confidence: float = Field(ge=0.0, le=1.0)
    resolution_reason: ResolutionRule

    # Human-readable expansion of `resolution_reason`.
    resolution_detail: str

    # Audit fields.
    resolved_at: datetime
    considered_event_ids: List[str] = Field(default_factory=list)
    ordered_event_ids: List[str] = Field(default_factory=list)
    duplicate_event_ids: List[str] = Field(default_factory=list)
    ignored_event_ids: List[str] = Field(default_factory=list)
    flip_detected: bool = False
    out_of_order_detected: bool = False
    silence_seconds: Optional[float] = None
    resolution_log: List[ResolutionLogEntry] = Field(default_factory=list)
