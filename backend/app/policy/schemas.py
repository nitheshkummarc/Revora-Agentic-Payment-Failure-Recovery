"""Schemas for policy validation.

`EventContext` types every compliance field as Optional. That is deliberate:
absence has to be representable so it can fail closed. A non-optional field
with a default would silently manufacture a passing value for an event that
never carried one, which is how a compliance gap stays invisible.
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

    Money fields are in paise, matching Razorpay's integer-paise convention.
    `mandate_ceiling` is paise for the same reason: keeping it consistent with
    `amount` is the only choice that avoids a silent rupee/paise comparison
    inside the ceiling check.
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
    # Selects which AFA threshold applies. Free-form text rather than an enum:
    # an unrecognised value falls back to the general threshold, which is the
    # stricter of the two, so an unknown category can never relax the rule.
    mandate_category: Optional[str] = None

    # --- dunning / opt-out ---
    opted_out: bool = False
    retry_count: int = Field(
        default=0,
        ge=0,
        description="Recovery attempts already made for this payment",
    )
    discount_amount: int = Field(default=0, ge=0, description="Proposed discount in paise")

    # The tracer's confidence. It is not carried on the recommendation, so the
    # orchestrator supplies it here. Optional: when absent, the defence-in-depth
    # confidence rule records itself as skipped rather than silently passing.
    trace_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    evaluated_at: Optional[datetime] = None


class RuleEvaluation(StrictModel):
    """One rule's result, recorded whether it passed or failed, so an audit
    shows everything that was checked and not only what tripped."""

    rule_id: str
    passed: bool
    detail: Optional[str] = None


class PolicyDecision(StrictModel):
    """The policy verdict.

    `approved`, `blocked_reason` and `final_action` are the contract; everything
    else is audit detail for the dashboard.
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
