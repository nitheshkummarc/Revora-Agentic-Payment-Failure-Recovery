"""Schemas for the PolicyEngine (Module 5).

`EventContext` types every RBI field as Optional. That is deliberate: "missing"
has to be representable, because GROUND_TRUTH.md Day 6-7 requires a missing
field to fail closed. A non-optional field with a default would silently
manufacture a passing value for an event that never carried one -- which is
exactly the "compliance gap invisible until judged" failure mode.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.intelligence.schemas import RecommendedAction


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventContext(StrictModel):
    """The compliance facts about one payment.

    Money fields are in PAISE, matching Razorpay's integer-paise convention and
    the `amount` field the Days 11-13 dataset spec requires. `mandate_ceiling`
    is paise for the same reason -- GROUND_TRUTH.md does not state its unit, and
    making it consistent with `amount` is the only choice that avoids a
    silent rupee/paise comparison inside the ceiling check.
    """

    payment_id: str
    amount: int = Field(ge=0, description="Action amount in paise (50000 = Rs.500)")
    currency: str = "INR"

    # --- RBI fields. Optional so that absence is representable and blocks. ---
    pre_debit_notice_sent_at: Optional[datetime] = None
    mandate_ceiling: Optional[int] = Field(
        default=None, ge=0, description="Customer-set ceiling in paise"
    )
    afa_flag: Optional[bool] = None

    # --- dunning / opt-out ---
    opted_out: bool = False
    retry_count: int = Field(
        default=0,
        ge=0,
        description="Recovery attempts already made (Module 1's failed_charge_attempts)",
    )
    discount_amount: int = Field(default=0, ge=0, description="Proposed discount in paise")

    # Module 3's tracer confidence. It is NOT on the Intelligence Layer's
    # output, and Module 5 may not modify intelligence/, so the Orchestrator
    # supplies it here. Optional: when it is not supplied the defence-in-depth
    # confidence rule records itself as skipped rather than silently passing.
    trace_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    evaluated_at: Optional[datetime] = None


class RuleEvaluation(StrictModel):
    """One rule's result, recorded whether it passed or failed, so the audit
    trail shows what was checked -- not only what tripped."""

    rule_id: str
    passed: bool
    detail: Optional[str] = None


class PolicyDecision(StrictModel):
    """The PolicyEngine's verdict.

    The three fields the Module 5 prompt specifies are `approved`,
    `blocked_reason` and `final_action`. Everything else is audit detail for
    the X-Ray dashboard.
    """

    payment_id: str

    # --- the three required fields ---
    approved: bool
    blocked_reason: Optional[str] = None
    final_action: RecommendedAction

    # --- audit ---
    rule_id: Optional[str] = None
    recommended_action: RecommendedAction
    overrode_llm: bool = False
    rules_evaluated: List[RuleEvaluation] = Field(default_factory=list)
    rbi_circular: Optional[str] = None
    llm_confidence: Optional[float] = None
    trace_confidence: Optional[float] = None
    injection_patterns_flagged: List[str] = Field(default_factory=list)
    decided_at: datetime
